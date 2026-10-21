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
#include <limits.h>
#include <time.h>
#include <unistd.h>

static int fail(int code,int line){fprintf(stderr,"Collector stopped: code=%d line=%d\n",code,line);return code;}
static const char *process_name="com.tencent.mobileqq:MSF";
static const char *out_of_scope_process_name="com.tencent.mobileqq";
struct owner_scan {unsigned processes,uid_matches,fd_matches,allowed_matches,foreign_matches;int allowed_pid,fd_pid,foreign_pid;unsigned long long foreign_start;char foreign_name[128];};
#define OWNER_RETRY_DELAY_US 10000
#define OWNER_INITIAL_ATTEMPTS 10
#define OWNER_MAX_ATTEMPTS 20
#define MAX_PENDING_SEGMENTS 64
#define MAX_PENDING_BYTES 65536
#ifdef IMPAD_COLLECTOR_TESTING
static char test_proc_root[PATH_MAX]="/proc";
#define PROC_ROOT test_proc_root
typedef int (*test_owner_lookup_fn)(unsigned,unsigned long long,unsigned long long *,struct owner_scan *);
static test_owner_lookup_fn test_owner_lookup;
typedef unsigned long long (*test_socket_lookup_fn)(const uint8_t *,const uint8_t *,int,unsigned,unsigned,unsigned *);
static test_socket_lookup_fn test_socket_lookup;
#else
#define PROC_ROOT "/proc"
#endif
static double monotonic(void){struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return t.tv_sec+t.tv_nsec/1e9;}
static unsigned long long monotonic_ns(void){struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return (unsigned long long)t.tv_sec*1000000000ULL+(unsigned long long)t.tv_nsec;}
static uint16_t be16(const uint8_t *p){return ((uint16_t)p[0]<<8)|p[1];}
static uint32_t be32(const uint8_t *p){return ((uint32_t)p[0]<<24)|((uint32_t)p[1]<<16)|((uint32_t)p[2]<<8)|p[3];}
static int text(const char *path,char *buf,size_t size){FILE *f=fopen(path,"r");if(!f)return 0;size_t n=fread(buf,1,size-1,f);fclose(f);buf[n]=0;return n>0;}
static unsigned long long started(int pid){
 char path[PATH_MAX],buf[2048];snprintf(path,sizeof(path),"%s/%d/stat",PROC_ROOT,pid);if(!text(path,buf,sizeof(buf)))return 0;
 char *p=strrchr(buf,')'),*save=0;if(!p)return 0;
 p=strtok_r(p+1," ",&save);for(int field=3;p;p=strtok_r(0," ",&save),field++)if(field==22)return strtoull(p,0,10);
 return 0;
}
static int process_matches_name(int pid,unsigned uid,const char *name){
 char path[PATH_MAX],buf[128];struct stat st;snprintf(path,sizeof(path),"%s/%d",PROC_ROOT,pid);
 if(stat(path,&st)||st.st_uid!=uid)return 0;
 snprintf(path,sizeof(path),"%s/%d/cmdline",PROC_ROOT,pid);
 return text(path,buf,sizeof(buf))&&strcmp(buf,name)==0;
}
static int process_matches(int pid,unsigned uid){return process_matches_name(pid,uid,process_name);}
static int owns_fd(int pid,unsigned long long inode){
 char path[PATH_MAX],wanted[64],link[128];snprintf(path,sizeof(path),"%s/%d/fd",PROC_ROOT,pid);
 DIR *d=opendir(path);if(!d)return 0;snprintf(wanted,sizeof(wanted),"socket:[%llu]",inode);
 struct dirent *e;int found=0;
 while((e=readdir(d))){
  if(e->d_name[0]<'0'||e->d_name[0]>'9')continue;
  snprintf(path,sizeof(path),"%s/%d/fd/%s",PROC_ROOT,pid,e->d_name);
  ssize_t n=readlink(path,link,sizeof(link)-1);if(n<0)continue;link[n]=0;
  if(!strcmp(link,wanted)){found=1;break;}
 }
 closedir(d);return found;
}
static int find_owner(unsigned uid,unsigned long long inode,unsigned long long *birth,struct owner_scan *scan){
 memset(scan,0,sizeof(*scan));
 DIR *d=opendir(PROC_ROOT);if(!d)return 0;struct dirent *e;int pid=0;unsigned long long candidate_birth=0;
 while((e=readdir(d))){
  if(e->d_name[0]<'0'||e->d_name[0]>'9')continue;
  int candidate=atoi(e->d_name);
  char path[PATH_MAX];struct stat st;snprintf(path,sizeof(path),"%s/%d",PROC_ROOT,candidate);
  scan->processes++;
  if(candidate>1&&!stat(path,&st)&&st.st_uid==uid){
   scan->uid_matches++;
   if(!owns_fd(candidate,inode))continue;
   scan->fd_matches++;scan->fd_pid=candidate;
   unsigned long long before=started(candidate);
   char cmdline[128],cmdpath[PATH_MAX];snprintf(cmdpath,sizeof(cmdpath),"%s/%d/cmdline",PROC_ROOT,candidate);
   int readable=text(cmdpath,cmdline,sizeof(cmdline));
   if(!readable||!before||started(candidate)!=before||!owns_fd(candidate,inode)||
      !process_matches_name(candidate,uid,cmdline))continue;
   if(!strcmp(cmdline,process_name)){
    scan->allowed_matches++;
    if(!pid){pid=candidate;candidate_birth=before;scan->allowed_pid=pid;}
   }else{
    scan->foreign_matches++;
    if(!scan->foreign_pid){scan->foreign_pid=candidate;scan->foreign_start=before;size_t n=0;
     while(cmdline[n]&&n+1<sizeof(scan->foreign_name)){unsigned char ch=(unsigned char)cmdline[n];
      scan->foreign_name[n]=(ch>='a'&&ch<='z')||(ch>='A'&&ch<='Z')||(ch>='0'&&ch<='9')||ch=='.'||ch=='_'||ch==':'||ch=='-'?(char)ch:'_';n++;
     }
     scan->foreign_name[n]=0;
    }
   }
  }
 }
 closedir(d);
 if(scan->fd_matches!=1||scan->allowed_matches>1||scan->foreign_matches>1)return 0;
 if(pid&&scan->allowed_matches==1&&scan->foreign_matches==0){*birth=candidate_birth;return pid;}
 if(!pid&&scan->foreign_matches==1&&scan->foreign_pid>0&&
    strcmp(scan->foreign_name,out_of_scope_process_name)==0)return -1;
 return 0;
}
static int owner_lookup(unsigned uid,unsigned long long inode,unsigned long long *birth,struct owner_scan *scan){
#ifdef IMPAD_COLLECTOR_TESTING
 if(test_owner_lookup)return test_owner_lookup(uid,inode,birth,scan);
#endif
 return find_owner(uid,inode,birth,scan);
}
static int owner_identity_matches(int pid,unsigned uid,unsigned long long birth,unsigned long long inode){
 unsigned long long before=started(pid);
 if(!birth||before!=birth||!process_matches(pid,uid)||!owns_fd(pid,inode))return 0;
 return process_matches(pid,uid)&&started(pid)==birth&&owns_fd(pid,inode);
}
static int proc_address(const char *value,const uint8_t *address,int bytes,unsigned port){
 const char *colon=strchr(value,':');if(!colon||(int)(colon-value)!=bytes*2||strtoul(colon+1,0,16)!=port)return 0;
 // Linux /proc prints each address word in native byte order.
 for(int i=0;i<bytes;i+=4){char word[9];memcpy(word,value+i*2,8);word[8]=0;uint32_t n=strtoul(word,0,16);if(memcmp(&n,address+i,4))return 0;}
 return 1;
}
static unsigned long long socket_inode(const uint8_t *source,const uint8_t *destination,int bytes,unsigned sp,unsigned dp,unsigned *uid){
 char path[PATH_MAX];snprintf(path,sizeof(path),"%s/net/%s",PROC_ROOT,bytes==4?"tcp":"tcp6");
 FILE *f=fopen(path,"r");if(!f)return 0;
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
#ifdef IMPAD_COLLECTOR_TESTING
 if(test_socket_lookup)return test_socket_lookup(source,destination,bytes,sp,dp,uid);
#endif
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
 unsigned long long birth,inode,foreign_birth;unsigned generation,socket_uid,owner_attempts;int socket_seen,foreign_pid;
 struct pending_segment *pending_head,*pending_tail;unsigned pending_count;size_t pending_bytes;
};
struct pending_segment {
 struct pending_segment *next;uint32_t sequence;unsigned flags,generation;uint64_t observed_ns;
 size_t payload_size;uint8_t *payload;
};
#define OWNER_UNKNOWN 0
#define OWNER_ALLOWED 1
#define OWNER_DISALLOWED -1
#define OWNER_OUT_OF_SCOPE -2
#define OWNER_IDENTITY_CHANGED -3
enum {FLOW_PACKET_NOT_DISCARDED=0,FLOW_PACKET_DISCARD=1};
static int flow_socket_identity_matches(const struct flow_owner *flow,unsigned uid){
 unsigned socket_uid=0;unsigned long long inode=lookup_socket(flow->source,flow->destination,flow->bytes,flow->sp,flow->dp,&socket_uid);
 return inode==flow->inode&&socket_uid==uid&&flow->socket_uid==uid;
}
static int flow_owner_identity_matches(const struct flow_owner *flow,unsigned uid){
 struct owner_scan scan={0};unsigned long long birth=0;
 if(flow->pid<=0||flow->socket_uid!=uid||!flow_socket_identity_matches(flow,uid))return 0;
 int result=owner_lookup(uid,flow->inode,&birth,&scan);
 int unique=result==flow->pid&&scan.fd_matches==1&&scan.allowed_matches==1&&
            scan.foreign_matches==0&&scan.fd_pid==flow->pid&&scan.allowed_pid==flow->pid&&
            birth==flow->birth&&flow->birth!=0;
 int process_current=unique&&owner_identity_matches(flow->pid,uid,flow->birth,flow->inode);
 int socket_current=unique&&flow_socket_identity_matches(flow,uid);
 int identity=unique&&process_current&&socket_current;
 fprintf(stderr,"OWNER_RECHECK pid=%d uid=%u start=%llu inode=%llu result=%s unique=%d scannedBirth=%llu fdPid=%d allowedPid=%d fdMatches=%u allowedMatches=%u foreignMatches=%u processCurrent=%d socketCurrent=%d\n",
         flow->pid,uid,flow->birth,flow->inode,identity?"same-unique-owner":
         result==OWNER_UNKNOWN?"unknown":result==OWNER_DISALLOWED?"disallowed":
         result==OWNER_ALLOWED?"owner-changed":"socket-identity-changed",unique,birth,scan.fd_pid,scan.allowed_pid,
         scan.fd_matches,scan.allowed_matches,scan.foreign_matches,process_current,socket_current);
 return identity;
}
static int out_of_scope_packet_action(const struct flow_owner *flow){
 return flow->pid==OWNER_DISALLOWED?FLOW_PACKET_DISCARD:FLOW_PACKET_NOT_DISCARDED;
}
static int starts_new_flow_generation(const struct flow_owner *known,unsigned flags,unsigned sequence){
 if(!(flags&2)||(flags&16))return 0;
 if(!known||known->generation!=sequence)return 1;
 unsigned socket_uid=0;
 unsigned long long inode=lookup_socket(known->source,known->destination,known->bytes,known->sp,known->dp,&socket_uid);
 return !known->socket_seen||!inode||inode!=known->inode||socket_uid!=known->socket_uid;
}
static int scan_flow_owner(struct flow_owner *flow,unsigned uid,struct owner_scan *scan){
 unsigned socket_uid=0;unsigned long long inode=lookup_socket(flow->source,flow->destination,flow->bytes,flow->sp,flow->dp,&socket_uid);
 if(!inode)return OWNER_UNKNOWN;
 if(flow->socket_seen&&(inode!=flow->inode||socket_uid!=flow->socket_uid))return OWNER_IDENTITY_CHANGED;
 if(!flow->socket_seen){flow->socket_seen=1;flow->inode=inode;flow->socket_uid=socket_uid;}
 if(socket_uid!=uid)return OWNER_OUT_OF_SCOPE;
 unsigned long long birth=0;int pid=owner_lookup(uid,inode,&birth,scan);
 if(pid>0){flow->pid=pid;flow->birth=birth;return OWNER_ALLOWED;}
 if(pid<0){flow->foreign_pid=scan->foreign_pid;flow->foreign_birth=scan->foreign_start;return OWNER_DISALLOWED;}
 return OWNER_UNKNOWN;
}
static int resolve_flow_owner(struct flow_owner *flow,unsigned uid,unsigned budget,const char *phase){
 int result=OWNER_UNKNOWN;
 for(unsigned i=0;i<budget&&flow->owner_attempts<OWNER_MAX_ATTEMPTS;i++){
  struct owner_scan scan={0};flow->owner_attempts++;
  result=scan_flow_owner(flow,uid,&scan);
  char source[INET6_ADDRSTRLEN],destination[INET6_ADDRSTRLEN];
  inet_ntop(flow->bytes==4?AF_INET:AF_INET6,flow->source,source,sizeof(source));
  inet_ntop(flow->bytes==4?AF_INET:AF_INET6,flow->destination,destination,sizeof(destination));
  fprintf(stderr,"OWNER_SCAN timeNs=%llu phase=%s attempt=%u tuple=%s:%u>%s:%u generation=%u targetUid=%u socketUid=%u inode=%llu result=%s pid=%d start=%llu processes=%u uidMatches=%u fdMatches=%u allowedMatches=%u fdPid=%d foreignMatches=%u foreignPid=%d foreignStart=%llu foreignName=%s\n",
          monotonic_ns(),phase,flow->owner_attempts,source,flow->sp,destination,flow->dp,flow->generation,uid,
          flow->socket_uid,flow->inode,result==OWNER_ALLOWED?"allowed":result==OWNER_DISALLOWED?"disallowed":result==OWNER_OUT_OF_SCOPE?"out-of-scope":result==OWNER_IDENTITY_CHANGED?"identity-changed":"unknown",
          flow->pid,flow->birth,scan.processes,scan.uid_matches,scan.fd_matches,scan.allowed_matches,scan.fd_pid,scan.foreign_matches,scan.foreign_pid,scan.foreign_start,
          scan.foreign_name[0]?scan.foreign_name:"-");
  if(result!=OWNER_UNKNOWN)return result;
  if(flow->owner_attempts<OWNER_MAX_ATTEMPTS&&i+1<budget)usleep(OWNER_RETRY_DELAY_US);
 }
 return result;
}
static int pending_append(struct flow_owner *flow,uint32_t sequence,unsigned flags,unsigned generation,
                          uint64_t observed_ns,const uint8_t *payload,size_t payload_size){
 if(flow->pending_count>=MAX_PENDING_SEGMENTS||payload_size>MAX_PENDING_BYTES-flow->pending_bytes)return 1;
 struct pending_segment *segment=calloc(1,sizeof(*segment));if(!segment)return 1;
 if(payload_size){segment->payload=malloc(payload_size);if(!segment->payload){free(segment);return 1;}memcpy(segment->payload,payload,payload_size);}
 segment->sequence=sequence;segment->flags=flags;segment->generation=generation;
 segment->observed_ns=observed_ns;segment->payload_size=payload_size;
 if(flow->pending_tail)flow->pending_tail->next=segment;else flow->pending_head=segment;
 flow->pending_tail=segment;flow->pending_count++;flow->pending_bytes+=payload_size;return 0;
}
static int write_segment(FILE *out,int pid,unsigned uid,unsigned long long birth,unsigned long long inode,
                         const uint8_t *source,const uint8_t *destination,int bytes,unsigned sp,unsigned dp,
                         const struct pending_segment *segment){
 if(fprintf(out,"{\"kind\":\"segment\",\"observedMonotonicNs\":%llu,\"pid\":%d,\"uid\":%u,\"started\":%llu,\"inode\":%llu,\"source\":\"",
            (unsigned long long)segment->observed_ns,pid,uid,birth,inode)<0)return 1;
 for(int i=0;i<bytes;i++)if(fprintf(out,"%02x",source[i])<0)return 1;
 if(fprintf(out,"\",\"destination\":\"")<0)return 1;
 for(int i=0;i<bytes;i++)if(fprintf(out,"%02x",destination[i])<0)return 1;
 if(fprintf(out,"\",\"sourcePort\":%u,\"destinationPort\":%u,\"sequence\":%u,\"flags\":%u,\"generation\":%u,\"payload\":\"",
            sp,dp,segment->sequence,segment->flags,segment->generation)<0)return 1;
 for(size_t i=0;i<segment->payload_size;i++)if(fprintf(out,"%02x",segment->payload[i])<0)return 1;
 return fprintf(out,"\"}\n")<0||fflush(out)||ferror(out);
}
static int pending_emit(struct flow_owner *flow,unsigned uid,FILE *out){
 struct pending_segment *segment=flow->pending_head;
 if(flow->socket_uid!=uid||flow->pid<=0)return 1;
 while(segment){
  if(!flow_owner_identity_matches(flow,uid))return 1;
  if(write_segment(out,flow->pid,uid,flow->birth,flow->inode,flow->source,flow->destination,
                   flow->bytes,flow->sp,flow->dp,segment))return 1;
  segment=segment->next;
 }
 while(flow->pending_head){segment=flow->pending_head->next;free(flow->pending_head->payload);free(flow->pending_head);flow->pending_head=segment;}
 flow->pending_tail=0;flow->pending_count=0;flow->pending_bytes=0;return 0;
}
static int emit_current(struct flow_owner *flow,unsigned uid,uint32_t sequence,unsigned flags,
                        uint64_t observed_ns,const uint8_t *payload,size_t payload_size){
 if(flow->socket_uid!=uid||flow->pid<=0||!flow_owner_identity_matches(flow,uid))return 1;
 struct pending_segment segment={.sequence=sequence,.flags=flags,.generation=flow->generation,
                                .observed_ns=observed_ns,.payload_size=payload_size,
                                .payload=(uint8_t *)payload};
 return write_segment(stdout,flow->pid,uid,flow->birth,flow->inode,flow->source,flow->destination,
                      flow->bytes,flow->sp,flow->dp,&segment);
}
static void pending_clear(struct flow_owner *flow){
 while(flow->pending_head){struct pending_segment *next=flow->pending_head->next;free(flow->pending_head->payload);free(flow->pending_head);flow->pending_head=next;}
 flow->pending_tail=0;flow->pending_count=0;flow->pending_bytes=0;
}
static struct flow_owner flows[512];static unsigned flow_count;
static struct flow_owner *cached(const uint8_t *source,const uint8_t *destination,int bytes,unsigned sp,unsigned dp){
 for(unsigned i=0;i<flow_count;i++){
  struct flow_owner *f=&flows[i];
  if(f->bytes==bytes&&f->sp==sp&&f->dp==dp&&!memcmp(f->source,source,bytes)&&!memcmp(f->destination,destination,bytes))return f;
 }
 return 0;
}
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
  uint64_t observed_ns=monotonic_ns();
  unsigned sp=be16(tcp),dp=be16(tcp+2),generation=be32(tcp+4);
  struct flow_owner *known=cached(source,destination,bytes,sp,dp);
  int new_syn=starts_new_flow_generation(known,flags,generation);
  if(new_syn){
   if(known&&known->pending_head){fprintf(stderr,"OWNER_FAIL reason=new-generation-before-pending-owner-resolved\n");return fail(8,__LINE__);}
   if(!known){if(flow_count>=512)return fail(5,__LINE__);known=&flows[flow_count++];}
   else pending_clear(known);
   memset(known,0,sizeof(*known));memcpy(known->source,source,bytes);memcpy(known->destination,destination,bytes);
   known->bytes=bytes;known->sp=sp;known->dp=dp;known->generation=generation;
   int result=resolve_flow_owner(known,(unsigned)uid,OWNER_INITIAL_ATTEMPTS,"syn");
   if(result==OWNER_OUT_OF_SCOPE){known->pid=OWNER_OUT_OF_SCOPE;continue;}
   if(result==OWNER_DISALLOWED){known->pid=OWNER_DISALLOWED;continue;}
   if(result==OWNER_IDENTITY_CHANGED){fprintf(stderr,"OWNER_FAIL reason=socket-identity-changed-on-syn\n");return fail(8,__LINE__);}
   if(result==OWNER_UNKNOWN){
    if(pending_append(known,generation,flags,generation,observed_ns,tcp+header,payload_size)){
     fprintf(stderr,"OWNER_FAIL reason=pending-syn-buffer-limit generation=%u bytes=%zu\n",generation,payload_size);return fail(8,__LINE__);
    }
    if(payload_size&&known->owner_attempts>=OWNER_MAX_ATTEMPTS){fprintf(stderr,"OWNER_FAIL reason=owner-timeout-before-syn-payload generation=%u bytes=%zu\n",generation,payload_size);return fail(8,__LINE__);}
    continue;
   }
   if(!owner_identity_matches(known->pid,(unsigned)uid,known->birth,known->inode)){
    fprintf(stderr,"OWNER_FAIL reason=identity-attestation-failed phase=syn pid=%d inode=%llu\n",known->pid,known->inode);return fail(8,__LINE__);
   }
   if(emit_current(known,(unsigned)uid,generation,flags,observed_ns,tcp+header,payload_size))return fail(8,__LINE__);
   if(++events>4096)return fail(5,__LINE__);
   continue;
  }
  if(!known){
   unsigned untracked_uid=0;unsigned long long untracked_inode=lookup_socket(source,destination,bytes,sp,dp,&untracked_uid);
   if(payload_size&&(!untracked_inode||untracked_uid==(unsigned)uid)){
    char src[INET6_ADDRSTRLEN],dst[INET6_ADDRSTRLEN];inet_ntop(bytes==4?AF_INET:AF_INET6,source,src,sizeof(src));inet_ntop(bytes==4?AF_INET:AF_INET6,destination,dst,sizeof(dst));
    struct tpacket_stats current={0};socklen_t current_len=sizeof(current);int statistics=getsockopt(fd,SOL_PACKET,PACKET_STATISTICS,&current,&current_len);
    fprintf(stderr,"Untracked payload source=%s:%u destination=%s:%u ifindex=%d flags=%u sequence=%u bytes=%zu uid=%u inode=%llu flows=%u statistics=%d packets=%u drops=%u\n",
            src,sp,dst,dp,address.sll_ifindex,flags,generation,payload_size,untracked_uid,untracked_inode,flow_count,statistics,current.tp_packets,current.tp_drops);
    return fail(8,__LINE__);
   }
   continue;
  }
  if(known->pid==OWNER_OUT_OF_SCOPE)continue;
  if(out_of_scope_packet_action(known)==FLOW_PACKET_DISCARD)continue;
  if(known->pid==OWNER_UNKNOWN){
   unsigned remaining=known->owner_attempts<OWNER_MAX_ATTEMPTS?OWNER_MAX_ATTEMPTS-known->owner_attempts:0;
   int result=remaining?resolve_flow_owner(known,(unsigned)uid,remaining,"deferred"):OWNER_UNKNOWN;
   if(result==OWNER_OUT_OF_SCOPE){
    if(known->pending_bytes){fprintf(stderr,"OWNER_FAIL reason=unresolved-payload-became-out-of-scope uid=%u inode=%llu generation=%u pendingBytes=%zu\n",known->socket_uid,known->inode,known->generation,known->pending_bytes);return fail(8,__LINE__);}
    known->pid=OWNER_OUT_OF_SCOPE;pending_clear(known);continue;
   }
   if(result==OWNER_DISALLOWED){
    size_t pending_bytes=known->pending_bytes;known->pid=OWNER_DISALLOWED;
    if(pending_bytes)fprintf(stderr,"OWNER_DISCARD reason=confirmed-main-process-out-of-scope-buffered pid=%d uid=%u start=%llu inode=%llu generation=%u pendingSegments=%u pendingBytes=%zu\n",
                            known->foreign_pid,known->socket_uid,known->foreign_birth,known->inode,known->generation,known->pending_count,pending_bytes);
    pending_clear(known);continue;
   }
   if(result==OWNER_IDENTITY_CHANGED){fprintf(stderr,"OWNER_FAIL reason=socket-identity-changed-during-recheck uid=%u inode=%llu generation=%u\n",known->socket_uid,known->inode,known->generation);return fail(8,__LINE__);}
   if(result==OWNER_UNKNOWN){if(payload_size){fprintf(stderr,"OWNER_FAIL reason=owner-unresolved-at-payload uid=%u inode=%llu generation=%u sequence=%u bytes=%zu attempts=%u\n",known->socket_uid,known->inode,known->generation,generation,payload_size,known->owner_attempts);return fail(8,__LINE__);}continue;}
   if(known->pending_count){events+=known->pending_count;if(events>4096)return fail(5,__LINE__);if(pending_emit(known,(unsigned)uid,stdout)){fprintf(stderr,"OWNER_FAIL reason=pending-event-identity-or-output-failed uid=%u inode=%llu generation=%u\n",known->socket_uid,known->inode,known->generation);return fail(8,__LINE__);}}
  }
  if(known->pid<=0)continue;
  if(!owner_identity_matches(known->pid,(unsigned)uid,known->birth,known->inode)){
   fprintf(stderr,"OWNER_FAIL reason=identity-changed-before-payload pid=%d uid=%u inode=%llu generation=%u sequence=%u bytes=%zu\n",known->pid,(unsigned)uid,known->inode,known->generation,generation,payload_size);return fail(8,__LINE__);
  }
  if(emit_current(known,(unsigned)uid,generation,flags,observed_ns,tcp+header,payload_size))return fail(8,__LINE__);
  if(++events>4096)return fail(5,__LINE__);
 }
 for(unsigned i=0;i<flow_count;i++){
  if(flows[i].pending_bytes){fprintf(stderr,"OWNER_FAIL reason=unresolved-pending-payload-at-capture-end uid=%u inode=%llu generation=%u pendingBytes=%zu\n",flows[i].socket_uid,flows[i].inode,flows[i].generation,flows[i].pending_bytes);return fail(8,__LINE__);}
  pending_clear(&flows[i]);
 }
 struct tpacket_stats stats;socklen_t len=sizeof(stats);if(getsockopt(fd,SOL_PACKET,PACKET_STATISTICS,&stats,&len)){perror("statistics");return 7;}
 printf("{\"kind\":\"end\",\"kernelDrops\":%u}\n",stats.tp_drops);if(fflush(stdout))return 6;
 close(fd);return 0;
}
