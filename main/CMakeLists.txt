idf_component_register(SRCS "main.cpp" "configuration.cpp" "network.cpp" "portal.cpp" "acquisition.cpp" "transport.cpp" "ble_transport.cpp" "codec.cpp"
    INCLUDE_DIRS "."
    REQUIRES esp_wifi esp_eth esp_netif esp_event esp_http_server nvs_flash esp_driver_gpio esp_driver_spi esp_timer esp_system esp_app_format mbedtls json bt mdns bootloader_support
    EMBED_TXTFILES "web/index.html"
    EMBED_FILES "web/logo.png")
