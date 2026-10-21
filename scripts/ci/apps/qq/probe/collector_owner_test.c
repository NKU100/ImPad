#define IMPAD_COLLECTOR_TESTING
#define main impad_collector_main
#include "collector.c"
#undef main

#include <assert.h>
#include <limits.h>

static int owner_results[8];
static unsigned owner_result_count;
static unsigned owner_result_index;
static unsigned recheck_lookup_count;
static unsigned long long fake_inode=953173;
static unsigned fake_socket_uid=10089;

static unsigned long long fake_socket_lookup(const uint8_t *source,const uint8_t *destination,
                                             int bytes,unsigned sp,unsigned dp,unsigned *uid) {
 (void)source;(void)destination;(void)bytes;(void)sp;(void)dp;*uid=fake_socket_uid;return fake_inode;
}

static int fake_owner_lookup(unsigned uid, unsigned long long inode,
                             unsigned long long *birth, struct owner_scan *scan) {
 (void)uid;
 (void)inode;
 memset(scan, 0, sizeof(*scan));
 scan->processes=3;
 if(owner_result_index>=owner_result_count)return 0;
 int result=owner_results[owner_result_index++];
 if(result>0){scan->fd_matches=1;scan->allowed_pid=result;*birth=777;}
 if(result<0){scan->foreign_matches=1;scan->foreign_pid=23456;scan->foreign_start=777;strcpy(scan->foreign_name,"com.tencent.mobileqq");}
 return result;
}

static int ambiguous_owner_lookup(unsigned uid,unsigned long long inode,
                                  unsigned long long *birth,struct owner_scan *scan) {
 (void)uid;(void)inode;(void)birth;recheck_lookup_count++;
 memset(scan,0,sizeof(*scan));scan->fd_matches=2;scan->allowed_matches=1;scan->foreign_matches=1;
 return OWNER_UNKNOWN;
}

static void reset_fake_results(const int *results,unsigned count) {
 memcpy(owner_results,results,count*sizeof(*results));
 owner_result_count=count;
 owner_result_index=0;
 test_owner_lookup=fake_owner_lookup;
 test_socket_lookup=fake_socket_lookup;
}

static void make_fake_proc(char *path,size_t size) {
 char pattern[]="/tmp/impad-owner-test-XXXXXX";
 char *dir=mkdtemp(pattern);assert(dir);
 snprintf(path,size,"%s",dir);
 snprintf(test_proc_root,sizeof(test_proc_root),"%s",dir);
}

static void write_file(const char *path,const char *value) {
 FILE *file=fopen(path,"w");assert(file);assert(fputs(value,file)>=0);assert(fclose(file)==0);
}

static void make_process(const char *root,int pid,const char *name,unsigned long long start,unsigned long long inode) {
 char path[PATH_MAX],value[1024];
 snprintf(path,sizeof(path),"%s/%d",root,pid);assert(mkdir(path,0700)==0);
 snprintf(value,sizeof(value),"%s/cmdline",path);write_file(value,name);
 snprintf(value,sizeof(value),"%s/stat",path);
 char stat_text[512];int used=snprintf(stat_text,sizeof(stat_text),"%d (%s) S",pid,name);
 for(int i=0;i<18;i++)used+=snprintf(stat_text+used,sizeof(stat_text)-(size_t)used," 0");
 snprintf(stat_text+used,sizeof(stat_text)-(size_t)used," %llu\n",start);write_file(value,stat_text);
 snprintf(path,sizeof(path),"%s/%d/fd",root,pid);assert(mkdir(path,0700)==0);
 char target[64];snprintf(target,sizeof(target),"socket:[%llu]",inode);
 snprintf(value,sizeof(value),"%s/3",path);assert(symlink(target,value)==0);
}

static void remove_process(const char *root,int pid) {
 char path[PATH_MAX];
 snprintf(path,sizeof(path),"%s/%d/cmdline",root,pid);assert(unlink(path)==0);
 snprintf(path,sizeof(path),"%s/%d/stat",root,pid);assert(unlink(path)==0);
 snprintf(path,sizeof(path),"%s/%d/fd/3",root,pid);assert(unlink(path)==0);
 snprintf(path,sizeof(path),"%s/%d/fd",root,pid);assert(rmdir(path)==0);
 snprintf(path,sizeof(path),"%s/%d",root,pid);assert(rmdir(path)==0);
}

static char *read_stream(FILE *stream) {
 assert(fflush(stream)==0);assert(fseek(stream,0,SEEK_END)==0);long size=ftell(stream);assert(size>=0);
 assert(fseek(stream,0,SEEK_SET)==0);char *data=calloc((size_t)size+1,1);assert(data);
 assert(fread(data,1,(size_t)size,stream)==(size_t)size);return data;
}

int main(void) {
 unsigned long long birth=0;int transient[]={0,0,23456};
 reset_fake_results(transient,3);
 struct flow_owner resolved={0};resolved.source[0]=10;resolved.destination[0]=1;resolved.bytes=4;resolved.sp=1234;resolved.dp=8080;resolved.generation=99;
 assert(resolve_flow_owner(&resolved,10089,10,"test-transient")==OWNER_ALLOWED);
 assert(owner_result_index==3&&resolved.birth==777&&resolved.pid==23456&&resolved.inode==953173&&resolved.owner_attempts==3);

 int unknown[]={0,0,0};reset_fake_results(unknown,3);
 struct flow_owner unresolved={0};unresolved.source[0]=10;unresolved.destination[0]=1;unresolved.bytes=4;unresolved.sp=1234;unresolved.dp=8080;unresolved.generation=100;
 assert(resolve_flow_owner(&unresolved,10089,3,"test-unknown")==OWNER_UNKNOWN);
 assert(owner_result_index==3);

 int foreign[]={-1,23456};reset_fake_results(foreign,2);
 struct flow_owner disallowed={0};disallowed.source[0]=10;disallowed.destination[0]=1;disallowed.bytes=4;disallowed.sp=1234;disallowed.dp=8080;disallowed.generation=101;
 assert(resolve_flow_owner(&disallowed,10089,10,"test-disallowed")==OWNER_DISALLOWED);
 assert(owner_result_index==1);

 reset_fake_results(transient,3);fake_socket_uid=0;struct flow_owner outside={0};outside.source[0]=10;outside.destination[0]=1;outside.bytes=4;outside.sp=1234;outside.dp=8080;outside.generation=103;
 assert(resolve_flow_owner(&outside,10089,10,"test-out-of-scope")==OWNER_OUT_OF_SCOPE);
 assert(owner_result_index==0);fake_socket_uid=10089;

 reset_fake_results(transient,3);struct flow_owner changed={0};changed.source[0]=10;changed.destination[0]=1;changed.bytes=4;changed.sp=1234;changed.dp=8080;changed.generation=102;
 assert(resolve_flow_owner(&changed,10089,1,"test-inode-change")==OWNER_UNKNOWN);
 fake_inode=953174;
 assert(resolve_flow_owner(&changed,10089,1,"test-inode-change")==OWNER_IDENTITY_CHANGED);
 fake_inode=953173;

 char root[PATH_MAX];make_fake_proc(root,sizeof(root));make_process(root,23456,"com.tencent.mobileqq:MSF",777,953173);unsigned test_uid=(unsigned)getuid();
 assert(owner_identity_matches(23456,test_uid,777,953173));
 assert(!owner_identity_matches(23456,test_uid,778,953173));
 assert(!owner_identity_matches(23456,test_uid+1,777,953173));
 assert(!owner_identity_matches(23456,test_uid,777,123));
 struct owner_scan actual_scan={0};birth=0;test_owner_lookup=NULL;test_socket_lookup=NULL;
 assert(find_owner(test_uid,953173,&birth,&actual_scan)==23456&&birth==777);
 assert(actual_scan.fd_matches==1&&actual_scan.allowed_matches==1&&actual_scan.allowed_pid==23456);
 make_process(root,23457,"com.tencent.mobileqq:worker",778,953173);
 birth=0;assert(find_owner(test_uid,953173,&birth,&actual_scan)==OWNER_UNKNOWN);
 assert(actual_scan.foreign_matches==1&&actual_scan.allowed_matches==1);
 remove_process(root,23457);
 make_process(root,23457,"com.tencent.mobileqq:MSF",778,953173);
 birth=0;assert(find_owner(test_uid,953173,&birth,&actual_scan)==OWNER_UNKNOWN);
 assert(actual_scan.allowed_matches==2);
 remove_process(root,23457);
 char path[PATH_MAX];snprintf(path,sizeof(path),"%s/23456/cmdline",root);
 write_file(path,"com.tencent.mobileqq");
 make_process(root,23457,"com.tencent.mobileqq:MSF",778,953173);
 birth=0;assert(find_owner(test_uid,953173,&birth,&actual_scan)==OWNER_UNKNOWN);
 assert(actual_scan.foreign_matches==1&&actual_scan.allowed_matches==1);
 remove_process(root,23457);
 write_file(path,"com.tencent.mobileqq:MSF");
 write_file(path,"com.tencent.mobileqq:worker");
 birth=0;assert(find_owner(test_uid,953173,&birth,&actual_scan)==OWNER_UNKNOWN);
 assert(actual_scan.foreign_matches==1&&strcmp(actual_scan.foreign_name,"com.tencent.mobileqq:worker")==0);
 write_file(path,"com.tencent.mobileqq");
 write_file(path,"com.tencent.mobileqq:worker\nfake");
 fake_socket_uid=test_uid;test_socket_lookup=fake_socket_lookup;
 struct flow_owner foreign_flow={0};foreign_flow.source[0]=10;foreign_flow.destination[0]=1;foreign_flow.bytes=4;foreign_flow.sp=3456;foreign_flow.dp=8080;foreign_flow.generation=105;
 assert(resolve_flow_owner(&foreign_flow,test_uid,1,"test-foreign-owner")==OWNER_UNKNOWN);
 assert(foreign_flow.owner_attempts==1);
 test_socket_lookup=NULL;fake_socket_uid=10089;
 write_file(path,"com.tencent.mobileqq");
 assert(!owner_identity_matches(23456,test_uid,777,953173));
 birth=0;assert(find_owner(test_uid,953173,&birth,&actual_scan)==OWNER_DISALLOWED);
 assert(actual_scan.foreign_pid==23456&&strcmp(actual_scan.foreign_name,"com.tencent.mobileqq")==0);

 struct flow_owner main_flow={0};memcpy(main_flow.source,"\x0a\0\0\x02",4);memcpy(main_flow.destination,"\x01\x02\x03\x04",4);
 main_flow.bytes=4;main_flow.sp=3456;main_flow.dp=8080;main_flow.generation=105;
 fake_socket_uid=test_uid;fake_inode=953173;test_socket_lookup=fake_socket_lookup;
 test_owner_lookup=NULL;
 assert(scan_flow_owner(&main_flow,test_uid,&actual_scan)==OWNER_DISALLOWED);
 main_flow.pid=OWNER_DISALLOWED;
 assert(main_flow.pid==OWNER_DISALLOWED&&main_flow.foreign_pid==23456&&main_flow.foreign_birth==777&&main_flow.inode==953173);
 struct flow_owner closing_main=main_flow;
 assert(out_of_scope_packet_action(&closing_main,106,0x10,37)==FLOW_PACKET_DISCARD);
 assert(out_of_scope_packet_action(&closing_main,143,0x11,0)==FLOW_PACKET_DISCARD);
 assert(closing_main.foreign_fin_seen);
 assert(closing_main.foreign_fin_end==144);
 fake_inode=0; /* matching tuple is now represented by TIME_WAIT/no owner inode */
 assert(out_of_scope_packet_action(&closing_main,144,0x10,0)==FLOW_PACKET_DISCARD);
 assert(out_of_scope_packet_action(&closing_main,106,0x10,37)==FLOW_PACKET_DISCARD);
 assert(out_of_scope_packet_action(&closing_main,144,0x10,12)==FLOW_PACKET_AFTER_CLOSE);
 assert(starts_new_flow_generation(&closing_main,0x02,closing_main.generation));
 fake_inode=953173;
 int reused_owner[]={-1};reset_fake_results(reused_owner,1);
 struct flow_owner reused_main={0};memcpy(reused_main.source,closing_main.source,closing_main.bytes);
 memcpy(reused_main.destination,closing_main.destination,closing_main.bytes);reused_main.bytes=closing_main.bytes;
 reused_main.sp=closing_main.sp;reused_main.dp=closing_main.dp;reused_main.generation=closing_main.generation;
 assert(scan_flow_owner(&reused_main,test_uid,&actual_scan)==OWNER_DISALLOWED&&owner_result_index==1);
 test_owner_lookup=NULL;
 struct flow_owner unknown_initial={0};unknown_initial.pid=OWNER_UNKNOWN;unknown_initial.socket_uid=test_uid;
 fake_inode=0;
 assert(out_of_scope_packet_action(&unknown_initial,144,0x10,12)==FLOW_PACKET_NOT_DISCARDED);
 assert(!unknown_initial.foreign_fin_seen);
 fake_inode=953173;
 struct flow_owner unclosed_main=main_flow;fake_inode=0;
 assert(out_of_scope_packet_action(&unclosed_main,144,0x10,12)==FLOW_PACKET_IDENTITY_FAILURE);
 fake_inode=953173;
 struct flow_owner reset_main=main_flow;
 assert(out_of_scope_packet_action(&reset_main,143,0x15,0)==FLOW_PACKET_DISCARD);
 assert(!reset_main.foreign_fin_seen);
 fake_inode=0;
 assert(out_of_scope_packet_action(&reset_main,144,0x10,1)==FLOW_PACKET_IDENTITY_FAILURE);
 fake_inode=953173;
 assert(out_of_scope_packet_action(&main_flow,106,16,37)==FLOW_PACKET_DISCARD);
 assert(!starts_new_flow_generation(&main_flow,0x18,106));
 assert(starts_new_flow_generation(&main_flow,0x02,106));
 assert(!starts_new_flow_generation(&main_flow,0x12,106));
 struct flow_owner unknown_flow=main_flow;unknown_flow.pid=OWNER_UNKNOWN;unknown_flow.foreign_pid=0;
 assert(out_of_scope_packet_action(&unknown_flow,107,16,37)==FLOW_PACKET_NOT_DISCARDED);
 FILE *unknown_output=tmpfile();assert(unknown_output);
 assert(pending_append(&unknown_flow,107,16,105,1000007,(const unsigned char *)"x",1)==0);
 assert(pending_emit(&unknown_flow,test_uid,unknown_output)!=0);
 char *unknown_bytes=read_stream(unknown_output);assert(unknown_bytes[0]=='\0');free(unknown_bytes);fclose(unknown_output);pending_clear(&unknown_flow);
 main_flow.foreign_birth=778;
 assert(out_of_scope_packet_action(&main_flow,108,16,37)==FLOW_PACKET_IDENTITY_FAILURE);
 main_flow.foreign_birth=777;
 write_file(path,"com.tencent.mobileqq:MSF");
 assert(out_of_scope_packet_action(&main_flow,109,16,37)==FLOW_PACKET_IDENTITY_FAILURE);
 write_file(path,"com.tencent.mobileqq");

 write_file(path,"com.tencent.mobileqq:MSF");
 struct flow_owner flow={0};flow.pid=23456;flow.birth=777;flow.inode=953173;flow.socket_uid=test_uid;
 memcpy(flow.source,"\x0a\0\0\x02",4);memcpy(flow.destination,"\x01\x02\x03\x04",4);flow.bytes=4;flow.sp=3456;flow.dp=8080;
 struct flow_owner first_unique={0};memcpy(first_unique.source,"\x0a\0\0\x02",4);memcpy(first_unique.destination,"\x01\x02\x03\x04",4);
 first_unique.bytes=4;first_unique.sp=3456;first_unique.dp=8080;fake_inode=953173;fake_socket_uid=test_uid;
 assert(scan_flow_owner(&first_unique,test_uid,&actual_scan)==OWNER_ALLOWED);
 assert(flow_socket_identity_matches(&first_unique,test_uid));
 assert(owner_identity_matches(first_unique.pid,test_uid,first_unique.birth,first_unique.inode));
 assert(first_unique.pid==23456&&first_unique.birth==777&&flow_owner_identity_matches(&first_unique,test_uid));
 fake_inode=0;
 assert(!flow_owner_identity_matches(&first_unique,test_uid));
 assert(out_of_scope_packet_action(&first_unique,109,16,5)==FLOW_PACKET_NOT_DISCARDED);
 fake_inode=953173;
 make_process(root,23457,"com.tencent.mobileqq",778,953173);
 assert(!flow_owner_identity_matches(&first_unique,test_uid));
 FILE *later_main_output=tmpfile();assert(later_main_output);
 assert(pending_append(&first_unique,99,16,first_unique.generation,999999, (const unsigned char *)"late",4)==0);
 assert(pending_emit(&first_unique,test_uid,later_main_output)!=0);
 char *later_main_bytes=read_stream(later_main_output);assert(later_main_bytes[0]=='\0');free(later_main_bytes);fclose(later_main_output);pending_clear(&first_unique);
 remove_process(root,23457);
 struct flow_owner first_unique_msf={0};memcpy(first_unique_msf.source,"\x0a\0\0\x02",4);memcpy(first_unique_msf.destination,"\x01\x02\x03\x04",4);
 first_unique_msf.bytes=4;first_unique_msf.sp=3456;first_unique_msf.dp=8080;
 assert(scan_flow_owner(&first_unique_msf,test_uid,&actual_scan)==OWNER_ALLOWED);
 assert(flow_socket_identity_matches(&first_unique_msf,test_uid));
 assert(owner_identity_matches(first_unique_msf.pid,test_uid,first_unique_msf.birth,first_unique_msf.inode));
 assert(first_unique_msf.pid==23456&&first_unique_msf.birth==777&&flow_owner_identity_matches(&first_unique_msf,test_uid));
 fake_inode=0;
 assert(!flow_owner_identity_matches(&first_unique_msf,test_uid));
 FILE *missing_msf_output=tmpfile();assert(missing_msf_output);
 assert(pending_append(&first_unique_msf,101,16,first_unique_msf.generation,1000001,(const unsigned char *)"msf",3)==0);
 assert(pending_emit(&first_unique_msf,test_uid,missing_msf_output)!=0);
 char *missing_msf_bytes=read_stream(missing_msf_output);assert(missing_msf_bytes[0]=='\0');
 free(missing_msf_bytes);fclose(missing_msf_output);pending_clear(&first_unique_msf);
 fake_inode=953173;
 make_process(root,23457,"com.tencent.mobileqq:MSF",778,953173);
 assert(!flow_owner_identity_matches(&first_unique_msf,test_uid));
 FILE *later_msf_output=tmpfile();assert(later_msf_output);
 assert(pending_append(&first_unique_msf,100,16,first_unique_msf.generation,1000000,(const unsigned char *)"msf",3)==0);
 assert(pending_emit(&first_unique_msf,test_uid,later_msf_output)!=0);
 char *later_msf_bytes=read_stream(later_msf_output);assert(later_msf_bytes[0]=='\0');free(later_msf_bytes);fclose(later_msf_output);pending_clear(&first_unique_msf);
 remove_process(root,23457);
 recheck_lookup_count=0;test_owner_lookup=ambiguous_owner_lookup;
 assert(!flow_owner_identity_matches(&first_unique,test_uid));
 assert(recheck_lookup_count==1);
 test_owner_lookup=NULL;
 const unsigned char early[]={0x51,0x51};
 assert(pending_append(&flow,100,2,9,1000000,early,sizeof(early))==0);
 assert(pending_append(&flow,102,16,9,1000002,NULL,0)==0);
 fake_inode=953173;fake_socket_uid=test_uid;test_socket_lookup=fake_socket_lookup;
 FILE *capture=tmpfile();assert(capture);int saved=dup(STDOUT_FILENO);assert(saved>=0);assert(dup2(fileno(capture),STDOUT_FILENO)>=0);
 assert(pending_emit(&flow,test_uid,capture)==0);
 assert(dup2(saved,STDOUT_FILENO)>=0);close(saved);
 char *output=read_stream(capture);fclose(capture);
 char *first=strstr(output,"\"sequence\":100");char *second=strstr(output,"\"sequence\":102");
 assert(first&&second&&first<second);
 assert(strstr(output,"\"observedMonotonicNs\":1000000"));
 assert(strstr(output,"\"payload\":\"5151\""));
 assert(!strstr(output,"\"kind\":\"end\""));
 free(output);
 assert(!starts_new_flow_generation(&flow,0x18,101));
 assert(flow_owner_identity_matches(&flow,test_uid));
 assert(pending_append(&flow,103,0x18,100,1000004,early,sizeof(early))==0);
 fake_inode=953174;
 assert(!flow_owner_identity_matches(&flow,test_uid));
 FILE *reused_tuple_output=tmpfile();assert(reused_tuple_output);
 assert(pending_emit(&flow,test_uid,reused_tuple_output)!=0);
 char *reused_bytes=read_stream(reused_tuple_output);assert(reused_bytes[0]=='\0');free(reused_bytes);fclose(reused_tuple_output);fake_inode=953173;pending_clear(&flow);
 make_process(root,23457,"com.tencent.mobileqq:MSF",778,953174);
 FILE *independent_capture=tmpfile();assert(independent_capture);
 struct flow_owner allowed_parallel=flow;allowed_parallel.pid=23457;allowed_parallel.birth=778;allowed_parallel.inode=953174;
 const unsigned char msf_payload[]={0x4d,0x53,0x46};fake_inode=953174;
 assert(pending_append(&allowed_parallel,101,0x18,100,1000003,msf_payload,sizeof(msf_payload))==0);
 assert(pending_emit(&allowed_parallel,test_uid,independent_capture)==0);
 char *parallel_output=read_stream(independent_capture);assert(strstr(parallel_output,"\"payload\":\"4d5346\""));free(parallel_output);fclose(independent_capture);
 remove_process(root,23457);

 struct flow_owner changed_birth={0};changed_birth.pid=23456;changed_birth.birth=778;changed_birth.inode=953173;changed_birth.socket_uid=test_uid;
 assert(pending_append(&changed_birth,104,16,9,1000004,early,sizeof(early))==0);
 FILE *rejected_identity=tmpfile();assert(rejected_identity);
 assert(pending_emit(&changed_birth,test_uid,rejected_identity)!=0);
 char *identity_output=read_stream(rejected_identity);assert(identity_output[0]=='\0');
 free(identity_output);fclose(rejected_identity);pending_clear(&changed_birth);

 struct flow_owner changed_inode={0};changed_inode.pid=23456;changed_inode.birth=777;changed_inode.inode=953174;changed_inode.socket_uid=test_uid;
 assert(pending_append(&changed_inode,106,16,9,1000006,early,sizeof(early))==0);
 FILE *rejected_inode=tmpfile();assert(rejected_inode);
 assert(pending_emit(&changed_inode,test_uid,rejected_inode)!=0);
 char *inode_output=read_stream(rejected_inode);assert(inode_output[0]=='\0');
 free(inode_output);fclose(rejected_inode);pending_clear(&changed_inode);

 struct flow_owner overflow={0};unsigned char *large=calloc(MAX_PENDING_BYTES+1,1);assert(large);
 assert(pending_append(&overflow,1,2,1,1,large,MAX_PENDING_BYTES+1)!=0);free(large);
 struct flow_owner still_unknown={0};still_unknown.socket_uid=test_uid;still_unknown.inode=953173;
 assert(pending_append(&still_unknown,1,2,1,2000000,early,sizeof(early))==0);
 FILE *rejected=tmpfile();assert(rejected);assert(pending_emit(&still_unknown,test_uid,rejected)!=0);
 char *rejected_output=read_stream(rejected);assert(rejected_output[0]=='\0');free(rejected_output);fclose(rejected);pending_clear(&still_unknown);
 remove_process(root,23456);assert(rmdir(root)==0);
 puts("collector-owner-tests: PASS");
 return 0;
}
