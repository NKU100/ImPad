#define _GNU_SOURCE
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/poll.h>
#include <net/if.h>
#include <linux/if_packet.h>
#include <linux/if_ether.h>
#include <arpa/inet.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <time.h>
#include <unistd.h>

static int fail(int code,int line){fprintf(stderr,"Collector stopped: code=%d line=%d\n",code,line);return code;}
static const char *process_name="com.tencent.mobileqq:MSF";
static double monotonic(void){struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return t.tv_sec+t.tv_nsec/1e9;}
static uint16_t be16(const uint8_t *p){return ((uint16_t)p[0]<<8)|p[1];}
static uint32_t be32(const uint8_t *p){return ((uint32_t)p[0]<<24)|((uint32_t)p[1]<<16)|((uint32_t)p[2]<<8)|p[3];}
static int text(const char *path,char *buf,size_t size){FILE *f=fopen(path,"r");if(!f)return 0;size_t n=fread(buf,1,size-1,f);fclose(f);buf[n]=0;return n>0;}
static unsigned long long started(int pid){
 char path[64],buf[2048];snprintf(path,sizeof(path),"/proc/%d/stat",pid);if(!text(path,buf,sizeof(buf)))return 0;
 char *p=strrchr(buf,')'),*save=0;if(!p)return 0;
 p=strtok_r(p+1," ",&save);for(int field=3;p;p=strtok_r(0," ",&save),field++)if(field==22)return strtoull(p,0,10);
 return 0;
}
static int process_matches(int pid,unsigned uid){
 char path[64],buf[128];struct stat st;snprintf(path,sizeof(path),"/proc/%d",pid);
 if(stat(path,&st)||st.st_uid!=uid)return 0;
 snprintf(path,sizeof(path),"/proc/%d/cmdline",pid);
 return text(path,buf,sizeof(buf))&&strcmp(buf,process_name)==0;
}
static int owns_fd(int pid,unsigned long long inode){
 char path[128],wanted[64],link[128];snprintf(path,sizeof(path),"/proc/%d/fd",pid);
 DIR *d=opendir(path);if(!d)return 0;snprintf(wanted,sizeof(wanted),"socket:[%llu]",inode);
 struct dirent *e;int found=0;
 while((e=readdir(d))){
  if(e->d_name[0]<'0'||e->d_name[0]>'9')continue;
  snprintf(path,sizeof(path),"/proc/%d/fd/%s",pid,e->d_name);
  ssize_t n=readlink(path,link,sizeof(link)-1);if(n<0)continue;link[n]=0;
  if(!strcmp(link,wanted)){found=1;break;}
 }
 closedir(d);return found;
}
static int find_owner(unsigned uid,unsigned long long inode,unsigned long long *birth){
 DIR *d=opendir("/proc");if(!d)return 0;struct dirent *e;int pid=0,foreign=0;
 while((e=readdir(d))){
  if(e->d_name[0]<'0'||e->d_name[0]>'9')continue;
  int candidate=atoi(e->d_name);
  char path[64];struct stat st;snprintf(path,sizeof(path),"/proc/%d",candidate);
  if(candidate>1&&!stat(path,&st)&&st.st_uid==uid&&owns_fd(candidate,inode)){
   if(!process_matches(candidate,uid)){foreign=1;continue;}
   unsigned long long before=started(candidate);
   if(before&&process_matches(candidate,uid)&&owns_fd(candidate,inode)&&started(candidate)==before){pid=candidate;*birth=before;break;}
  }
 }
 closedir(d);return pid?pid:(foreign?-1:0);
}
static int proc_address(const char *value,const uint8_t *address,int bytes,unsigned port){
 const char *colon=strchr(value,':');if(!colon||(int)(colon-value)!=bytes*2||strtoul(colon+1,0,16)!=port)return 0;
 // Linux /proc prints each address word in native byte order.
 for(int i=0;i<bytes;i+=4){char word[9];memcpy(word,value+i*2,8);word[8]=0;uint32_t n=strtoul(word,0,16);if(memcmp(&n,address+i,4))return 0;}
 return 1;
}
static unsigned long long socket_inode(const uint8_t *source,const uint8_t *destination,int bytes,unsigned sp,unsigned dp,unsigned *uid){
 FILE *f=fopen(bytes==4?"/proc/net/tcp":"/proc/net/tcp6","r");if(!f)return 0;
 char line[1024];unsigned long long inode=0;int count=0;
 while(fgets(line,sizeof(line),f)&&count++<8192){
  char *fields[10],*save=0,*p=strtok_r(line," \t\n",&save);int n=0;
  while(p&&n<10){fields[n++]=p;p=strtok_r(0," \t\n",&save);}
  if(n!=10)continue;
  if(proc_address(fields[1],source,bytes,sp)&&proc_address(fields[2],destination,bytes,dp)){inode=strtoull(fields[9],0,10);*uid=strtoul(fields[7],0,10);if(inode)break;}
 }
 fclose(f);return inode;
}
static unsigned long long lookup_socket(const uint8_t *source,const uint8_t *destination,int bytes,unsigned sp,unsigned dp,unsigned *uid){
 unsigned long long inode=socket_inode(source,destination,bytes,sp,dp,uid);
 if(!inode&&bytes==4){
  uint8_t a[16]={0},b[16]={0};a[10]=a[11]=b[10]=b[11]=255;
  memcpy(a+12,source,4);memcpy(b+12,destination,4);
  inode=socket_inode(a,b,16,sp,dp,uid);
 }
 return inode;
}
struct flow_owner {
 uint8_t source[16],destination[16];int bytes;unsigned sp,dp;int pid;
 unsigned long long birth,inode;unsigned generation;
};
static struct flow_owner flows[512];static unsigned flow_count;
static struct flow_owner *cached(const uint8_t *source,const uint8_t *destination,int bytes,unsigned sp,unsigned dp){
 for(unsigned i=0;i<flow_count;i++){
  struct flow_owner *f=&flows[i];
  if(f->bytes==bytes&&f->sp==sp&&f->dp==dp&&!memcmp(f->source,source,bytes)&&!memcmp(f->destination,destination,bytes))return f;
 }
 return 0;
}
static void hex(const uint8_t *p,size_t n){for(size_t i=0;i<n;i++)printf("%02x",p[i]);}
int main(int argc,char **argv){
 if(argc!=4||geteuid()!=0)return 2;
 char *end;unsigned long uid=strtoul(argv[1],&end,10);if(*end||uid<10000||uid>0x7fffffff)return 2;
 long seconds=strtol(argv[2],&end,10);if(*end||seconds<1||seconds>45)return 2;
 unsigned long long collector_birth=started(getpid());if(!collector_birth)return 2;
 int pid_fd=open(argv[3],O_WRONLY|O_CREAT|O_EXCL,0600);if(pid_fd<0)return 2;
 if(dprintf(pid_fd,"%d %llu\n",getpid(),collector_birth)<0||close(pid_fd))return 2;
 int fd=socket(AF_PACKET,SOCK_RAW,htons(ETH_P_ALL));if(fd<0){perror("socket");return 3;}
 int receive_size=4*1024*1024;if(setsockopt(fd,SOL_SOCKET,SO_RCVBUF,&receive_size,sizeof(receive_size))){perror("buffer");return 3;}
 printf("{\"kind\":\"ready\",\"uid\":%lu}\n",uid);fflush(stdout);
 double deadline=monotonic()+seconds;struct pollfd pollfd={.fd=fd,.events=POLLIN};uint8_t packet[65536];unsigned events=0;
 while(monotonic()<deadline){
  int ready=poll(&pollfd,1,100);if(ready<0){perror("poll");return 4;}if(!ready)continue;
  struct sockaddr_ll address;socklen_t length=sizeof(address);
  ssize_t size=recvfrom(fd,packet,sizeof(packet),MSG_TRUNC,(struct sockaddr*)&address,&length);
  if(size<0){perror("receive");return 4;}if(size>(ssize_t)sizeof(packet))return fail(5,__LINE__);
  if(address.sll_pkttype!=PACKET_OUTGOING||size<14)continue;
  uint16_t kind=be16(packet+12);if(kind==0x8100||kind==0x88a8)return fail(5,__LINE__);const uint8_t *ip=packet+14,*source,*destination,*tcp;int bytes;size_t total,header;
  if(kind==ETH_P_IP&&size>=34){
   header=(ip[0]&15)*4;total=be16(ip+2);if(header<20||total<header+20||total>(size_t)size-14||ip[9]!=6)continue;
   if(be16(ip+6)&0x3fff)return fail(5,__LINE__);
   source=ip+12;destination=ip+16;bytes=4;tcp=ip+header;total-=header;
  }else if(kind==ETH_P_IPV6&&size>=54){
   total=be16(ip+4);if(total>(size_t)size-54)continue;
   unsigned next=ip[6];size_t offset=0;int extensions=0;
   while(next==0||next==43||next==60||next==51||next==44){
    if(++extensions>8||offset+8>total)return fail(5,__LINE__);
    const uint8_t *extension=ip+40+offset;
    size_t extent=next==44?8:(next==51?(extension[1]+2)*4:(extension[1]+1)*8);
    if(next==44&&(be16(extension+2)&0xfff9))return fail(5,__LINE__);
    if(extent<8||offset+extent>total)return fail(5,__LINE__);
    next=extension[0];offset+=extent;
   }
   if(next==50)return fail(5,__LINE__);
   if(next==17||next==58||next==59)continue;
   if(next!=6)return fail(5,__LINE__);
   if(total-offset<20)return fail(5,__LINE__);
   source=ip+8;destination=ip+24;bytes=16;tcp=ip+40+offset;total-=offset;
  }else continue;
  header=(tcp[12]>>4)*4;if(header<20||header>total)continue;
  size_t payload_size=total-header;unsigned flags=tcp[13];if(!payload_size&&!(flags&2))continue;
  unsigned sp=be16(tcp),dp=be16(tcp+2);unsigned long long inode=0,birth=0;
  struct flow_owner *known=cached(source,destination,bytes,sp,dp);int pid=0;
  if((flags&2)&&!(flags&16)&&!(known&&known->generation==be32(tcp+4))){
   // Pending ownership may only survive while the flow carries no payload.
   if(known)known->pid=0;
   unsigned socket_uid=0;
   inode=lookup_socket(source,destination,bytes,sp,dp,&socket_uid);
   pid=!inode?0:(socket_uid==uid?find_owner(uid,inode,&birth):-1);
   for(int retry=0;inode&&!pid&&retry<10;retry++){usleep(10000);pid=find_owner(uid,inode,&birth);}
   
   
   if(!known){if(flow_count>=512)return fail(5,__LINE__);known=&flows[flow_count++];}
   memcpy(known->source,source,bytes);memcpy(known->destination,destination,bytes);
   known->bytes=bytes;known->sp=sp;known->dp=dp;known->pid=pid;known->birth=birth;known->inode=inode;known->generation=be32(tcp+4);
  }else{
   // QQ was stopped before capture; its outgoing client flows must have a SYN.
   if(!known){
    unsigned untracked_uid=0;
    unsigned long long untracked_inode=lookup_socket(source,destination,bytes,sp,dp,&untracked_uid);
    if(payload_size&&(!untracked_inode||untracked_uid==uid))return fail(8,__LINE__);
    continue;
   }
   if(!known->pid){if(payload_size){fprintf(stderr,"Unattributed TCP flow carried payload\n");return 8;}continue;}
   if(known->pid<0)continue;
   pid=known->pid;birth=known->birth;inode=known->inode;
   // Ownership is attested at SYN; queued tail packets may outlive the fd.
   if(!process_matches(pid,uid)||started(pid)!=birth){fprintf(stderr,"QQ socket process identity changed\n");return 8;}
  }
  if(!pid&&payload_size){fprintf(stderr,"Unattributed TCP SYN carried payload\n");return 8;}
  if(pid<=0)continue;
  if(++events>4096)return fail(5,__LINE__);
  printf("{\"kind\":\"segment\",\"pid\":%d,\"uid\":%lu,\"started\":%llu,\"inode\":%llu,\"source\":\"",pid,uid,birth,inode);hex(source,bytes);
  printf("\",\"destination\":\"");hex(destination,bytes);
  printf("\",\"sourcePort\":%u,\"destinationPort\":%u,\"sequence\":%u,\"flags\":%u,\"generation\":%u,\"payload\":\"",sp,dp,be32(tcp+4),flags,known->generation);hex(tcp+header,payload_size);printf("\"}\n");
  if(fflush(stdout)||ferror(stdout))return 6;
 }
 struct tpacket_stats stats;socklen_t len=sizeof(stats);if(getsockopt(fd,SOL_PACKET,PACKET_STATISTICS,&stats,&len)){perror("statistics");return 7;}
 printf("{\"kind\":\"end\",\"kernelDrops\":%u}\n",stats.tp_drops);if(fflush(stdout))return 6;
 close(fd);return 0;
}
