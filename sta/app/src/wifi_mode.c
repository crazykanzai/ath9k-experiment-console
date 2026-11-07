#include "wifi_mode.h"
#include "common.h"
#include "dhcp_ctrl.h"
#include "hostapd_ctrl.h"
#include "progress.h"
#include "wifi_nm.h"
#include "wifi_state.h"

#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

static void print_error_detail(struct progress *p, const char *operation,
                               const char *detail)
{
    progress_failure(p, "%s", operation);
    if (detail && detail[0])
        fprintf(stderr, "%s", detail);
}

static int safe_sta_rollback(const char *ifname, const char *pidfile,
                             struct progress *p)
{
    char err[OUTPUT_BUF_SIZE];
    bool dhcp_stopped;

    progress_step(p, 1, 5, "Rollback: stop DHCP and hostapd");
    dhcp_stopped = dhcp_stop(ifname, err, sizeof(err)) == 0;
    if (!dhcp_stopped)
        fprintf(stderr, "DHCP rollback failed: %s\n", err);
    (void)hostapd_stop(pidfile, 3000);
    progress_step(p, 2, 5, "Rollback: clear AP address");
    (void)wifi_link_set(ifname, false, err, sizeof(err));
    (void)wifi_addr_flush(ifname, err, sizeof(err));
    progress_step(p, 3, 5, "Rollback: set 802.11 STA type");
    (void)wifi_iw_set_managed(ifname, err, sizeof(err));
    progress_step(p, 4, 5, "Rollback: return interface to NetworkManager");
    (void)wifi_nm_set_managed(ifname, true, err, sizeof(err));
    progress_step(p, 5, 5, "Rollback: bring interface up");
    (void)wifi_link_set(ifname, true, err, sizeof(err));
    if (dhcp_stopped)
        progress_failure(p, "Transition failed; interface was returned to safe STA_IDLE state.");
    else
        progress_failure(p, "Transition failed; DHCP cleanup needs manual inspection.");
    return -1;
}

static int wait_ap(const char *ifname, const char *pidfile,
                   const char *ssid, const char *ip, unsigned int timeout_ms,
                   bool final)
{
    unsigned int waited = 0;

    while (waited <= timeout_ms) {
        struct wifi_state s;
        if (wifi_state_read(ifname, pidfile, &s) == 0) {
            if (!final && strcmp(s.iftype, "AP") == 0 && s.hostapd_alive)
                return 0;
            if (final && wifi_state_is_ap_ready(&s, ssid, ip))
                return 0;
        }
        sleep_ms(200);
        waited += 200;
    }
    errno = ETIMEDOUT;
    return -1;
}

int wifi_mode_to_ap(const char *ifname, const struct ap_options *options)
{
    struct progress p;
    struct hostapd_config_info cfg;
    struct wifi_state initial;
    char config[PATH_MAX];
    char pidfile[PATH_MAX];
    char err[OUTPUT_BUF_SIZE];

    progress_init(&p);
    if (geteuid() != 0) {
        progress_failure(&p, "Mode switching requires root; run with sudo.");
        errno = EACCES;
        return -1;
    }
    if (!options || !options->config_path || !options->ip_cidr ||
        validate_ipv4_cidr(options->ip_cidr) < 0) {
        progress_failure(&p, "Invalid AP options or IPv4/CIDR.");
        errno = EINVAL;
        return -1;
    }
    if (hostapd_resolve_config(options->config_path, config, sizeof(config)) < 0 ||
        hostapd_parse_config(config, &cfg) < 0) {
        progress_failure(&p, "Cannot read hostapd configuration: %s", strerror(errno));
        return -1;
    }
    if (strcmp(cfg.interface, ifname) != 0) {
        progress_failure(&p, "Config uses interface '%s', command uses '%s'.",
                         cfg.interface, ifname);
        errno = EINVAL;
        return -1;
    }
    if (make_pidfile_path(ifname, pidfile, sizeof(pidfile)) < 0)
        return -1;

    progress_step(&p, 1, 9, "Inspect current interface state");
    if (wifi_state_read(ifname, pidfile, &initial) < 0) {
        print_error_detail(&p, "Cannot inspect interface state", NULL);
        return -1;
    }
    if (wifi_state_is_ap_ready(&initial, cfg.ssid, options->ip_cidr)) {
        if (dhcp_start(ifname, options->ip_cidr, err, sizeof(err)) < 0) {
            print_error_detail(&p, "AP is ready but DHCP could not start", err);
            return -1;
        }
        progress_success(&p, "%s is already AP '%s' at %s; DHCP is running.",
                         ifname, cfg.ssid, options->ip_cidr);
        return 0;
    }

    progress_step(&p, 2, 9, "Clean stale hostapd state");
    if (hostapd_cleanup_stale_pidfile(pidfile) < 0) {
        print_error_detail(&p, "A live hostapd instance already owns this interface", NULL);
        return -1;
    }

    progress_step(&p, 3, 9, "Disconnect STA and release NetworkManager");
    (void)wifi_nm_disconnect(ifname, err, sizeof(err));
    if (wifi_nm_set_managed(ifname, false, err, sizeof(err)) < 0) {
        print_error_detail(&p, "NetworkManager did not release the interface", err);
        return -1;
    }

    progress_step(&p, 4, 9, "Bring interface down and clear old addresses");
    if (wifi_link_set(ifname, false, err, sizeof(err)) < 0 ||
        wifi_addr_flush(ifname, err, sizeof(err)) < 0) {
        print_error_detail(&p, "Failed to prepare interface", err);
        return safe_sta_rollback(ifname, pidfile, &p);
    }

    progress_step(&p, 5, 9, "Start hostapd for SSID '%s'", cfg.ssid);
    if (hostapd_start(config, pidfile, err, sizeof(err)) < 0) {
        print_error_detail(&p, "hostapd failed to start", err);
        return safe_sta_rollback(ifname, pidfile, &p);
    }

    progress_step(&p, 6, 9, "Wait for AP mode and Beacon service");
    if (wait_ap(ifname, pidfile, cfg.ssid, NULL, 5000, false) < 0) {
        print_error_detail(&p, "AP mode did not become ready within 5 seconds", NULL);
        return safe_sta_rollback(ifname, pidfile, &p);
    }

    progress_step(&p, 7, 9, "Assign AP address %s", options->ip_cidr);
    if (wifi_addr_replace(ifname, options->ip_cidr, err, sizeof(err)) < 0 ||
        wifi_link_set(ifname, true, err, sizeof(err)) < 0) {
        print_error_detail(&p, "Failed to configure AP address", err);
        return safe_sta_rollback(ifname, pidfile, &p);
    }

    progress_step(&p, 8, 9, "Verify hostapd, mode, SSID, link and IPv4");
    if (wait_ap(ifname, pidfile, cfg.ssid, options->ip_cidr, 5000, true) < 0) {
        print_error_detail(&p, "Final AP verification failed", NULL);
        return safe_sta_rollback(ifname, pidfile, &p);
    }

    progress_step(&p, 9, 9, "Start DHCP on %s", ifname);
    if (dhcp_start(ifname, options->ip_cidr, err, sizeof(err)) < 0) {
        print_error_detail(&p, "DHCP failed to start", err);
        return safe_sta_rollback(ifname, pidfile, &p);
    }

    progress_success(&p, "%s is AP '%s' at %s with DHCP.", ifname, cfg.ssid,
                     options->ip_cidr);
    return 0;
}

int wifi_mode_to_sta(const char *ifname)
{
    struct progress p;
    struct wifi_state s;
    char pidfile[PATH_MAX];
    char err[OUTPUT_BUF_SIZE];
    char connection[256];
    unsigned int waited = 0;

    progress_init(&p);
    if (geteuid() != 0) {
        progress_failure(&p, "Mode switching requires root; run with sudo.");
        errno = EACCES;
        return -1;
    }
    if (make_pidfile_path(ifname, pidfile, sizeof(pidfile)) < 0)
        return -1;

    progress_step(&p, 1, 7, "Inspect current interface state");
    if (wifi_state_read(ifname, pidfile, &s) < 0)
        return -1;

    progress_step(&p, 2, 7, "Stop project DHCP and hostapd");
    if (dhcp_stop(ifname, err, sizeof(err)) < 0) {
        print_error_detail(&p, "Could not stop DHCP", err);
        return -1;
    }
    if (hostapd_stop(pidfile, 3000) < 0) {
        print_error_detail(&p, "Could not stop hostapd", NULL);
        return -1;
    }

    progress_step(&p, 3, 7, "Bring interface down and clear AP address");
    if (wifi_link_set(ifname, false, err, sizeof(err)) < 0 ||
        wifi_addr_flush(ifname, err, sizeof(err)) < 0) {
        print_error_detail(&p, "Failed to clear AP state", err);
        return -1;
    }

    progress_step(&p, 4, 7, "Set 802.11 interface type to managed (STA)");
    if (wifi_iw_set_managed(ifname, err, sizeof(err)) < 0) {
        print_error_detail(&p, "Failed to set STA type", err);
        return -1;
    }

    progress_step(&p, 5, 7, "Return interface to NetworkManager");
    if (wifi_nm_set_managed(ifname, true, err, sizeof(err)) < 0 ||
        wifi_link_set(ifname, true, err, sizeof(err)) < 0) {
        print_error_detail(&p, "Failed to return interface to NetworkManager", err);
        return -1;
    }

    progress_step(&p, 6, 7, "Set an auto-connected STA profile to IPv4 DHCP");
    while (waited <= 3000) {
        if (wifi_nm_get_active_connection(ifname, connection,
                                          sizeof(connection)) == 0) {
            if (wifi_nm_enable_dhcp(ifname, err, sizeof(err)) < 0) {
                print_error_detail(&p, "Failed to enable DHCP on the active STA profile", err);
                return -1;
            }
            break;
        }
        sleep_ms(200);
        waited += 200;
    }

    progress_step(&p, 7, 7, "Verify safe STA state");
    sleep_ms(500);
    if (wifi_state_read(ifname, pidfile, &s) < 0 ||
        strcmp(s.iftype, "managed") != 0 || s.hostapd_alive) {
        print_error_detail(&p, "STA verification failed", NULL);
        errno = EIO;
        return -1;
    }

    progress_success(&p, "%s is now managed STA; NetworkManager may connect it.",
                     ifname);
    return 0;
}

int wifi_mode_sta_connect(const char *ifname,
                          const struct sta_connect_options *options)
{
    struct progress p;
    struct wifi_state s;
    char pidfile[PATH_MAX];
    char err[OUTPUT_BUF_SIZE];
    unsigned int waited = 0;

    progress_init(&p);
    if (geteuid() != 0) {
        progress_failure(&p, "STA connection requires root; run with sudo.");
        errno = EACCES;
        return -1;
    }
    if (!options || !options->ssid || !options->ssid[0]) {
        errno = EINVAL;
        return -1;
    }
    if (make_pidfile_path(ifname, pidfile, sizeof(pidfile)) < 0)
        return -1;

    progress_step(&p, 1, 4, "Ensure managed STA mode");
    if (wifi_state_read(ifname, pidfile, &s) < 0)
        return -1;
    if (strcmp(s.iftype, "managed") != 0 || s.hostapd_alive) {
        progress_clear(&p);
        if (wifi_mode_to_sta(ifname) < 0)
            return -1;
    }

    progress_step(&p, 2, 4, "Enable NetworkManager control");
    if (wifi_nm_set_managed(ifname, true, err, sizeof(err)) < 0 ||
        wifi_link_set(ifname, true, err, sizeof(err)) < 0) {
        print_error_detail(&p, "Failed to prepare STA", err);
        return -1;
    }

    progress_step(&p, 3, 4, "Connect to SSID '%s' and enforce IPv4 DHCP", options->ssid);
    if (wifi_nm_connect(ifname, options->ssid, options->password,
                        options->connection_name, err, sizeof(err)) < 0) {
        print_error_detail(&p, "NetworkManager connection failed", err);
        return -1;
    }

    progress_step(&p, 4, 4, "Wait for association and a DHCP IPv4 lease");
    while (waited <= 15000) {
        if (wifi_state_read(ifname, pidfile, &s) == 0 &&
            s.state == WIFI_STATE_STA_CONNECTED && s.ipv4[0]) {
            progress_success(&p, "%s connected to '%s' with DHCP IPv4 %s.",
                             ifname, s.ssid, s.ipv4);
            return 0;
        }
        sleep_ms(250);
        waited += 250;
    }

    print_error_detail(&p, "STA association or DHCP lease did not become ready within 15 seconds", NULL);
    errno = ETIMEDOUT;
    return -1;
}

int wifi_mode_to_sta_idle(const char *ifname)
{
    struct progress p;
    struct wifi_state s;
    char pidfile[PATH_MAX];
    char err[OUTPUT_BUF_SIZE];
    unsigned int waited = 0;

    progress_init(&p);
    if (geteuid() != 0) {
        progress_failure(&p, "STA disconnect requires root; run with sudo.");
        errno = EACCES;
        return -1;
    }
    if (make_pidfile_path(ifname, pidfile, sizeof(pidfile)) < 0)
        return -1;

    progress_step(&p, 1, 4, "Inspect current interface state");
    if (wifi_state_read(ifname, pidfile, &s) < 0)
        return -1;

    if (strcmp(s.iftype, "managed") != 0 || s.hostapd_alive) {
        progress_clear(&p);
        if (wifi_mode_to_sta(ifname) < 0)
            return -1;
    }

    progress_step(&p, 2, 4, "Ensure NetworkManager controls the STA interface");
    if (wifi_nm_set_managed(ifname, true, err, sizeof(err)) < 0 ||
        wifi_link_set(ifname, true, err, sizeof(err)) < 0) {
        print_error_detail(&p, "Failed to prepare managed STA", err);
        return -1;
    }

    progress_step(&p, 3, 4, "Disconnect the current Wi-Fi connection");
    (void)wifi_nm_disconnect(ifname, err, sizeof(err));

    progress_step(&p, 4, 4, "Verify STA_IDLE");
    while (waited <= 5000) {
        if (wifi_state_read(ifname, pidfile, &s) == 0 &&
            s.state == WIFI_STATE_STA_IDLE &&
            strcmp(s.iftype, "managed") == 0 && !s.hostapd_alive) {
            progress_success(&p,
                             "%s is STA_IDLE: managed, disconnected, no hostapd.",
                             ifname);
            return 0;
        }
        sleep_ms(200);
        waited += 200;
    }

    print_error_detail(&p, "Interface did not settle in STA_IDLE", NULL);
    errno = ETIMEDOUT;
    return -1;
}
