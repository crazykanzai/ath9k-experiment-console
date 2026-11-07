#ifndef ATH9K_EDCA_WIFI_MODE_H
#define ATH9K_EDCA_WIFI_MODE_H

struct ap_options {
    const char *config_path;
    const char *ip_cidr;
};

struct sta_connect_options {
    const char *ssid;
    const char *password;
    const char *connection_name;
};

int wifi_mode_to_ap(const char *ifname, const struct ap_options *options);
int wifi_mode_to_sta(const char *ifname);
int wifi_mode_to_sta_idle(const char *ifname);
int wifi_mode_sta_connect(const char *ifname,
                          const struct sta_connect_options *options);

#endif
