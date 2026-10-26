#include "node.h"
#include "esp_wifi.h"
#include "esp_eth.h"
#include "esp_eth_mac_spi.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "bootloader_random.h"
#include "driver/gpio.h"
#include "driver/spi_master.h"
#include "mdns.h"
#include "lwip/sockets.h"
#include "lwip/inet.h"
#include <string.h>

esp_netif_t *wifi_netif=nullptr,*eth_netif=nullptr;
static std::atomic<bool> ap_on{false};
std::atomic<uint32_t> wifi_disconnects{0},wifi_disconnect_reason{0};
static void dns_task(void*) {
  int fd=socket(AF_INET,SOCK_DGRAM,IPPROTO_UDP);
  sockaddr_in addr{};addr.sin_family=AF_INET;addr.sin_port=htons(53);inet_pton(AF_INET,"192.168.4.1",&addr.sin_addr);
  if(fd<0||bind(fd,(sockaddr*)&addr,sizeof(addr))<0) {if(fd>=0)close(fd);vTaskDelete(nullptr);return;}
  timeval timeout{1,0};setsockopt(fd,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof(timeout));
  uint8_t bytes[512];
  while(ap_on) {
    sockaddr_in peer{};socklen_t len=sizeof(peer);
    int n=recvfrom(fd,bytes,sizeof(bytes)-16,0,(sockaddr*)&peer,&len);
    if(n<17||bytes[4]!=0||bytes[5]!=1)continue;
    size_t p=12;
    while(p<size_t(n)&&bytes[p]&&bytes[p]<=63) p+=bytes[p]+1;
    if(p+5!=size_t(n)||bytes[p]!=0||bytes[p+1]!=0||bytes[p+2]!=1||bytes[p+3]!=0||bytes[p+4]!=1)continue;
    bytes[2]=0x81;bytes[3]=0x80;bytes[6]=0;bytes[7]=1;bytes[8]=bytes[9]=bytes[10]=bytes[11]=0;
    const uint8_t answer[]={0xc0,0x0c,0,1,0,1,0,0,0,0,0,4,192,168,4,1};
    memcpy(bytes+n,answer,sizeof(answer));sendto(fd,bytes,n+sizeof(answer),0,(sockaddr*)&peer,len);
  }
  close(fd);vTaskDelete(nullptr);
}
static void event(void*,esp_event_base_t base,int32_t id,void *data) {
  if(base==ETH_EVENT&&id==ETHERNET_EVENT_CONNECTED)ESP_LOGI("network","Ethernet link up");
  if(base==ETH_EVENT&&id==ETHERNET_EVENT_DISCONNECTED)ESP_LOGW("network","Ethernet link down");
  if(base==IP_EVENT&&(id==IP_EVENT_STA_GOT_IP||id==IP_EVENT_ETH_GOT_IP)) {
    auto *e=(ip_event_got_ip_t*)data;
    ESP_LOGI("network","%s IP " IPSTR,id==IP_EVENT_ETH_GOT_IP?"Ethernet":"Wi-Fi",IP2STR(&e->ip_info.ip));
    if(configured&&((id==IP_EVENT_STA_GOT_IP&&!strcmp(config.link,"wifi"))||(id==IP_EVENT_ETH_GOT_IP&&!strcmp(config.link,"ethernet")))) {
      esp_netif_set_default_netif(e->esp_netif);link_ready=true;
    }
  }
  if(base==WIFI_EVENT&&id==WIFI_EVENT_STA_DISCONNECTED) {
    auto *info=static_cast<wifi_event_sta_disconnected_t*>(data);
    wifi_disconnect_reason=info?info->reason:0;
    if(configured&&!strcmp(config.link,"wifi")) {
      ++wifi_disconnects;link_ready=false;receiver_ready=false;
      ESP_LOGW("network","Wi-Fi disconnected: reason=%lu; reconnecting",(unsigned long)wifi_disconnect_reason.load());
      esp_wifi_connect();
    }
  }
  if(base==ETH_EVENT&&id==ETHERNET_EVENT_DISCONNECTED&&configured&&!strcmp(config.link,"ethernet")) {link_ready=false;receiver_ready=false;}
}
void network_try_wifi(const char *ssid,const char *password) {
  esp_wifi_disconnect(); wifi_config_t station{};
  memcpy(station.sta.ssid,ssid,strlen(ssid));
  memcpy(station.sta.password,password,strlen(password));
  station.sta.pmf_cfg.capable=true;
  ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA,&station));esp_wifi_connect();
}
void finish_setup_ap() {
  // Called only after a receiver accepts the saved configuration.
  if(ap_on) { ap_on=false;esp_wifi_set_mode(!strcmp(config.link,"wifi")?WIFI_MODE_STA:WIFI_MODE_NULL);if(!strcmp(config.link,"ethernet"))bootloader_random_enable(); }
}
void network_start() {
  ESP_ERROR_CHECK(esp_netif_init());ESP_ERROR_CHECK(esp_event_loop_create_default());
  ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT,ESP_EVENT_ANY_ID,event,nullptr));
  ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT,ESP_EVENT_ANY_ID,event,nullptr));
  ESP_ERROR_CHECK(esp_event_handler_register(ETH_EVENT,ESP_EVENT_ANY_ID,event,nullptr));
  wifi_netif=esp_netif_create_default_wifi_sta();
  esp_netif_create_default_wifi_ap();
  wifi_init_config_t init=WIFI_INIT_CONFIG_DEFAULT();ESP_ERROR_CHECK(esp_wifi_init(&init));
  ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
  // Configured devices use their selected link directly; BOOT hold reopens setup.
  ap_on=true; // Recovery access until the saved receiver is ready.
  ESP_ERROR_CHECK(esp_wifi_set_mode(ap_on?WIFI_MODE_APSTA:(!strcmp(config.link,"wifi")?WIFI_MODE_STA:WIFI_MODE_NULL)));
  if(ap_on) {
    wifi_config_t ap{};strlcpy((char*)ap.ap.ssid,ap_name,sizeof(ap.ap.ssid));
    strlcpy((char*)ap.ap.password,hotspot_password,sizeof(ap.ap.password));
    ap.ap.authmode=WIFI_AUTH_WPA2_PSK;ap.ap.max_connection=3;ap.ap.channel=1;
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP,&ap));
  }
  ESP_ERROR_CHECK(esp_wifi_start());ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
  esp_netif_set_hostname(wifi_netif,hostname);
  // Default station and Ethernet netifs obtain their addresses through DHCP.
  if(configured&&!strcmp(config.link,"wifi"))network_try_wifi(config.ssid,config.wifi_password);
  if(ap_on)xTaskCreate(dns_task,"portalDNS",4096,nullptr,2,nullptr);

  spi_bus_config_t bus{};bus.mosi_io_num=5;bus.miso_io_num=6;bus.sclk_io_num=4;bus.quadwp_io_num=bus.quadhd_io_num=-1;
  ESP_ERROR_CHECK(spi_bus_initialize(SPI3_HOST,&bus,SPI_DMA_CH_AUTO));
  spi_device_interface_config_t device{};device.mode=0;device.clock_speed_hz=8000000;device.spics_io_num=7;device.queue_size=20;
  eth_w5500_config_t w=ETH_W5500_DEFAULT_CONFIG(SPI3_HOST,&device);w.int_gpio_num=16;
  eth_mac_config_t mac=ETH_MAC_DEFAULT_CONFIG();eth_phy_config_t phy=ETH_PHY_DEFAULT_CONFIG();phy.reset_gpio_num=15;
  mac.flags |= ETH_MAC_FLAG_PIN_TO_CORE; // network_start runs on CPU 0.
  esp_eth_mac_t *m=esp_eth_mac_new_w5500(&w,&mac);esp_eth_phy_t *p=esp_eth_phy_new_w5500(&phy);
  esp_eth_config_t ec=ETH_DEFAULT_CONFIG(m,p);esp_eth_handle_t handle=nullptr;
  ESP_ERROR_CHECK(esp_eth_driver_install(&ec,&handle));
  uint8_t ethmac[6];esp_read_mac(ethmac,ESP_MAC_ETH);ESP_ERROR_CHECK(esp_eth_ioctl(handle,ETH_CMD_S_MAC_ADDR,ethmac));
  esp_netif_config_t nc=ESP_NETIF_DEFAULT_ETH();eth_netif=esp_netif_new(&nc);
  ESP_ERROR_CHECK(esp_netif_attach(eth_netif,esp_eth_new_netif_glue(handle)));
  esp_netif_set_hostname(eth_netif,hostname);
  ESP_ERROR_CHECK(esp_eth_start(handle));
  ESP_ERROR_CHECK(mdns_init());mdns_hostname_set(hostname);mdns_instance_name_set(ap_name);
  mdns_service_add(nullptr,"_http","_tcp",80,nullptr,0);
}
