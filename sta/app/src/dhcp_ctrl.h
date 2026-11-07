#ifndef ATH9K_EDCA_DHCP_CTRL_H
#define ATH9K_EDCA_DHCP_CTRL_H

#include <stddef.h>

int dhcp_start(const char *ifname, const char *ap_ip_cidr,
               char *detail, size_t detail_size);
int dhcp_stop(const char *ifname, char *detail, size_t detail_size);
int dhcp_status(const char *ifname, char *detail, size_t detail_size);

#endif
