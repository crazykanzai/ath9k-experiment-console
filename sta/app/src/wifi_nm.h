#ifndef ATH9K_EDCA_WIFI_NM_H
#define ATH9K_EDCA_WIFI_NM_H

#include <stdbool.h>
#include <stddef.h>

int wifi_nm_set_managed(const char *ifname, bool managed,
                        char *error, size_t error_size);
int wifi_nm_disconnect(const char *ifname, char *error, size_t error_size);
int wifi_nm_connect(const char *ifname, const char *ssid,
                    const char *password, const char *connection_name,
                    char *error, size_t error_size);
int wifi_nm_get_active_connection(const char *ifname, char *connection,
                                  size_t connection_size);
int wifi_nm_enable_dhcp(const char *ifname, char *error, size_t error_size);
int wifi_link_set(const char *ifname, bool up, char *error, size_t error_size);
int wifi_addr_flush(const char *ifname, char *error, size_t error_size);
int wifi_addr_replace(const char *ifname, const char *cidr,
                      char *error, size_t error_size);
int wifi_iw_set_managed(const char *ifname, char *error, size_t error_size);

#endif
