#include "node.h"
#include "codec.h"
#include "esp_timer.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_random.h"
#include "esp_app_desc.h"
#include "lwip/sockets.h"
#include "lwip/netdb.h"
#include "lwip/tcp.h"
#include <errno.h>
#include <fcntl.h>
#include <string.h>
#include <stdlib.h>
namespace {
uint32_t reconnects=0,send_stalls=0;
char boot_id[17];
struct Tcp {
  int fd=-1;
  ~Tcp(){receiver_ready=false;pause_transport();if(fd>=0)close(fd);}
  bool connect(){
    char port[8];snprintf(port,sizeof(port),"%u",config.port);addrinfo hints{},*list=nullptr;hints.ai_family=AF_INET;hints.ai_socktype=SOCK_STREAM;
    if(getaddrinfo(config.host,port,&hints,&list))return false;
    fd=socket(AF_INET,SOCK_STREAM,IPPROTO_TCP);if(fd<0){freeaddrinfo(list);return false;}
    fcntl(fd,F_SETFL,O_NONBLOCK);int r=::connect(fd,list->ai_addr,list->ai_addrlen);freeaddrinfo(list);
    if(r<0&&errno!=EINPROGRESS)return false;
    fd_set set;FD_ZERO(&set);FD_SET(fd,&set);timeval wait{5,0};
    if(select(fd+1,nullptr,&set,nullptr,&wait)<=0)return false;
    int err=0;socklen_t len=sizeof(err);if(getsockopt(fd,SOL_SOCKET,SO_ERROR,&err,&len)||err)return false;
    fcntl(fd,F_SETFL,0);timeval timeout{10,0};setsockopt(fd,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof(timeout));setsockopt(fd,SOL_SOCKET,SO_SNDTIMEO,&timeout,sizeof(timeout));
    int one=1;setsockopt(fd,IPPROTO_TCP,TCP_NODELAY,&one,sizeof(one));
    setsockopt(fd,SOL_SOCKET,SO_KEEPALIVE,&one,sizeof(one));
    return true;
  }
  bool write(const uint8_t*p,size_t n){
    // The ADC has 13.1 s of bounded buffering. A transient 3 s Wi-Fi stall must
    // not discard that queue by tearing down an otherwise healthy TCP session.
    const int64_t started=esp_timer_get_time(),deadline=started+8000000;
    while(n){
      if(reconfiguring||!link_ready)return false;
      if(esp_timer_get_time()>=deadline){ESP_LOGW("sender","TCP send stalled for 8 s; reconnecting");return false;}
      int r=::send(fd,p,n,MSG_DONTWAIT);
      if(r>0){p+=r;n-=r;if(n)vTaskDelay(1);continue;}
      if(r<0&&errno==EINTR){vTaskDelay(1);continue;}
      if(r==0||(errno!=EAGAIN&&errno!=EWOULDBLOCK)){
        ESP_LOGW("sender","TCP send failed: errno=%d",errno);return false;
      }
      fd_set writable;FD_ZERO(&writable);FD_SET(fd,&writable);timeval wait{0,100000};
      int ready=select(fd+1,nullptr,&writable,nullptr,&wait);
      if(ready<0&&errno!=EINTR)return false;
      // lwIP can report writable while send still returns EAGAIN (for example
      // under pbuf pressure). select alone does not guarantee that we block.
      vTaskDelay(1);
    }
    if(esp_timer_get_time()-started>=100000)++send_stalls;
    return true;
  }
  bool begin_session(){
    char*cal=calibration_json();if(!cal)return false;
    char hello[1800];int n=snprintf(hello,sizeof(hello),"{\"protocol\":\"MSAC1\",\"device\":\"%s\",\"boot_id\":\"%s\",\"firmware\":\"%s\",\"rate\":%lu,\"calibration\":%s}\n",node_id,boot_id,esp_app_get_description()->version,(unsigned long)config.rate,cal);free(cal);
    if(n<=0||size_t(n)>=sizeof(hello)||!write((uint8_t*)hello,n))return false;
    // This is a protocol-ready reply, not authentication.
    char ack[3];size_t count=0;while(count<3){int r=::recv(fd,ack+count,3-count,0);if(r<0&&errno==EINTR)continue;if(r<=0)return false;count+=r;}return memcmp(ack,"OK\n",3)==0;
  }
};
void put(uint8_t*p,uint64_t v,int bytes){for(int i=0;i<bytes;++i)p[i]=v>>(8*i);}
size_t packet(uint8_t*out,const WaveBlock*wave,const char*status){
  memset(out,0,40);memcpy(out,"MSC1",4);out[4]=1;out[5]=wave?1:0;
  put(out+6,wave?8:0,2);put(out+8,wave?128:0,2);put(out+10,wave?wave->selectors:selectors(),2);
  put(out+12,wave?wave->epoch:epoch.load(),4);put(out+16,config.rate,4);put(out+20,wave?wave->first:0,8);put(out+28,wave?wave->time_us:esp_timer_get_time(),8);
  size_t length=wave?msai_encode(wave->samples,128,8,out+40,3600):strlen(status);
  if(!length||length>3600)return 0;
  if(!wave)memcpy(out+40,status,length);
  put(out+36,length,4);put(out+40+length,msai_crc32(out,40+length),4);return length+44;
}
void sender(void*){
  snprintf(boot_id,sizeof(boot_id),"%08lx%08lx",(unsigned long)esp_random(),(unsigned long)esp_random());
  bool ble=!strcmp(config.link,"ble");if(ble)ble_start();
  WaveBlock wave{};uint8_t bytes[3700];bool pending=false;
  for(;;){
    if(reconfiguring){vTaskDelay(pdMS_TO_TICKS(100));continue;}
    if(!ble&&!link_ready){vTaskDelay(pdMS_TO_TICKS(1000));continue;}
    Tcp tcp;
    if(!ble){
      if(!tcp.connect()){ESP_LOGW("sender","TCP connect failed: %s:%u; retrying",config.host,config.port);vTaskDelay(pdMS_TO_TICKS(3000));continue;}
      if(!tcp.begin_session()){ESP_LOGW("sender","Receiver handshake failed: %s:%u; expected MSAC1 OK",config.host,config.port);vTaskDelay(pdMS_TO_TICKS(3000));continue;}
      // Finish radio-mode changes before allowing the ADC controller to start.
      finish_setup_ap();
      if(!link_ready)continue;
      ++reconnects;receiver_ready=true;
    }
    uint64_t last=0,encode_total=0,send_total=0;uint32_t measured=0,encode_max=0,send_max=0;
    while(!reconfiguring&&(ble||receiver_ready)){
      // A nonempty backlog bypasses the queue wait. Give IDLE0 actual CPU time
      // even when every encode/send succeeds immediately. taskYIELD is not enough.
      vTaskDelay(1);
      if(!receiver_ready){vTaskDelay(pdMS_TO_TICKS(100));continue;}
      if(!pending)pending=xQueueReceive(wave_queue,&wave,pdMS_TO_TICKS(20))==pdPASS;
      if(pending&&(fault||wave.epoch!=epoch))pending=false;
      if(pending){
        uint64_t begin=esp_timer_get_time();size_t n=packet(bytes,&wave,nullptr);uint32_t encode_us=esp_timer_get_time()-begin;if(!n){fail(13);break;}
        begin=esp_timer_get_time();bool ok=ble?ble_send(bytes,n):tcp.write(bytes,n);uint32_t send_us=esp_timer_get_time()-begin;
        encode_total+=encode_us;send_total+=send_us;++measured;encode_max=encode_us>encode_max?encode_us:encode_max;send_max=send_us>send_max?send_us:send_max;
        // Retain the in-flight block on failure and retry the complete frame in
        // the next recording session. TCP cannot prove remote persistence;
        // consumers must treat session boundaries as possible overlap/gaps.
        if(!ok){receiver_ready=false;pause_transport();if(!ble)break;}else {pending=false;++sent_blocks;}
      }
      if(transport_paused&&receiver_ready&&!pending&&acquisition_idle()&&uxQueueMessagesWaiting(wave_queue)==0){
        transport_paused=false;
        ESP_LOGI("sender","Backlog drained; acquisition may resume in a new epoch");
      }
      uint64_t now=esp_timer_get_time();if(now-last>=1000000){
        wifi_ap_record_t ap{};const bool wifi=!strcmp(config.link,"wifi");
        const bool wifi_info=wifi&&esp_wifi_sta_get_ap_info(&ap)==ESP_OK;
        char status[1024];snprintf(status,sizeof(status),"{\"fault\":%lu,\"running\":%s,\"blocks\":%lu,\"overflows\":%lu,\"adc_crc\":%lu,\"missed_adc\":%lu,\"adc_recoveries\":%lu,\"adc_input_highwater\":%lu,\"queued_blocks\":%u,\"encode_us_avg\":%lu,\"encode_us_max\":%lu,\"send_us_avg\":%lu,\"send_us_max\":%lu,\"link\":\"%s\",\"wifi_rssi_dbm\":%d,\"wifi_channel\":%u,\"wifi_disconnects\":%lu,\"wifi_disconnect_reason\":%lu,\"tcp_sessions\":%lu,\"send_stalls\":%lu,\"queued_ms\":%lu,\"feedback\":%u,\"transport_paused\":%s,\"transport_pauses\":%lu,\"firmware\":\"%s\"}",(unsigned long)fault.load(),acquiring?"true":"false",(unsigned long)sent_blocks.load(),(unsigned long)queue_overflows.load(),(unsigned long)adc_crc_errors.load(),(unsigned long)missed_adc.load(),(unsigned long)adc_recoveries.load(),(unsigned long)adc_input_highwater.load(),unsigned(uxQueueMessagesWaiting(wave_queue)),(unsigned long)(measured?encode_total/measured:0),(unsigned long)encode_max,(unsigned long)(measured?send_total/measured:0),(unsigned long)send_max,config.link,wifi_info?ap.rssi:-127,wifi_info?ap.primary:0,(unsigned long)wifi_disconnects.load(),(unsigned long)wifi_disconnect_reason.load(),(unsigned long)reconnects,(unsigned long)send_stalls,(unsigned long)(uxQueueMessagesWaiting(wave_queue)*128000ULL/config.rate),unsigned(feedback()),transport_paused?"true":"false",(unsigned long)transport_pauses.load(),esp_app_get_description()->version);
        size_t n=packet(bytes,nullptr,status);bool ok=ble?ble_send(bytes,n):tcp.write(bytes,n);if(!ok){receiver_ready=false;pause_transport();if(!ble)break;}last=now;
      }
    }
  }
}
}
void transport_start(){
  // Keep network stack calls away from the ADC's core.
  xTaskCreatePinnedToCore(sender,"transport",24576,nullptr,3,nullptr,0);
}
