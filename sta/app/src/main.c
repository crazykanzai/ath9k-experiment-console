#include "common.h"
#include "dhcp_ctrl.h"
#include "driver_ctrl.h"
#include "edca_ctrl.h"
#include "hostapd_ctrl.h"
#include "prompt.h"
#include "wifi_mode.h"
#include "wifi_state.h"

#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static void usage(const char *prog)
{
    fprintf(stderr,
        "Usage:\n"
        "  %s <if> status\n"
        "  %s <if> driver status [--module-dir DIR]\n"
        "  %s <if> driver load [--module-dir DIR]\n"
        "  %s <if> driver unload\n"
        "  %s <if> driver reload [--module-dir DIR]\n"
        "  %s <if> mode ap [--config FILE] [--ip IPv4/CIDR]\n"
        "  %s <if> mode sta\n"
        "  %s <if> mode idle\n"
        "  %s <if> sta-connect --ssid SSID [--password PASS] [--name NAME]\n"
        "  %s <if> sta-idle\n"
        "  %s <if> show\n"
        "  %s <if> set <VO|VI|BE|BK> <AIFS> <CWmin> <CWmax>\n"
        "  %s <if> hw [VO|VI|BE|BK]\n\n"
        "Compatibility aliases:\n"
        "  %s <if> ap-start [CONFIG] [IP]\n"
        "  %s <if> ap-stop\n"
        "  %s <if> sta-mode\n"
        "  %s <if> sta-disconnect\n\n"
        "Default module directory: ./modules/$(uname -r)\n"
        "Override with --module-dir DIR or ATH9K_MODULE_DIR.\n",
        prog, prog, prog, prog, prog, prog, prog, prog, prog, prog,
        prog, prog, prog, prog, prog, prog, prog);
}

static const char *get_option(int argc, char **argv, const char *name)
{
    int i;
    for (i = 0; i + 1 < argc; i++)
        if (strcmp(argv[i], name) == 0)
            return argv[i + 1];
    return NULL;
}

static int command_status(const char *ifname)
{
    struct wifi_state s;
    char pidfile[PATH_MAX];
    char debug_dir[PATH_MAX];
    char edca_path[PATH_MAX];
    char dhcp_detail[OUTPUT_BUF_SIZE];

    if (make_pidfile_path(ifname, pidfile, sizeof(pidfile)) < 0 ||
        wifi_state_read(ifname, pidfile, &s) < 0)
        return -1;
    wifi_state_print(ifname, &s);

    if (strcmp(s.iftype, "AP") == 0) {
        int dhcp_ok = dhcp_status(ifname, dhcp_detail, sizeof(dhcp_detail));
        dhcp_detail[strcspn(dhcp_detail, "\r\n")] = '\0';
        printf("DHCP          : %s\n", dhcp_detail[0] ? dhcp_detail :
               (dhcp_ok == 0 ? "running" : "not running"));
    }

    printf("ath9k debugfs : ");
    if (edca_resolve_debug_dir(ifname, debug_dir, sizeof(debug_dir)) < 0 ||
        path_join(edca_path, sizeof(edca_path), debug_dir, "edca_ctrl") < 0 ||
        access(edca_path, R_OK | W_OK) < 0)
        puts("unavailable");
    else
        printf("available (%s)\n", debug_dir);
    return 0;
}

static int command_mode_ap(const char *ifname, int argc, char **argv)
{
    const char *config = get_option(argc, argv, "--config");
    const char *ip = get_option(argc, argv, "--ip");
    char ipbuf[64];
    struct ap_options options;

    if (!config)
        config = "./hostapd-ath9k.conf";
    if (!ip) {
        if (prompt_text("AP IPv4/CIDR", "192.168.50.1/24",
                        ipbuf, sizeof(ipbuf)) < 0) {
            fprintf(stderr, "--ip is required when stdin is not interactive.\n");
            errno = EINVAL;
            return -1;
        }
        ip = ipbuf;
    }
    options.config_path = config;
    options.ip_cidr = ip;
    return wifi_mode_to_ap(ifname, &options);
}

static int command_sta_connect(const char *ifname, int argc, char **argv)
{
    const char *ssid = get_option(argc, argv, "--ssid");
    const char *password = get_option(argc, argv, "--password");
    const char *name = get_option(argc, argv, "--name");
    char ssidbuf[128];
    char passbuf[256];
    struct sta_connect_options options;

    if (!ssid) {
        if (prompt_text("STA SSID", NULL, ssidbuf, sizeof(ssidbuf)) < 0)
            return -1;
        ssid = ssidbuf;
    }
    if (!password) {
        if (prompt_secret("Wi-Fi password", passbuf, sizeof(passbuf)) < 0) {
            fprintf(stderr, "--password is required when stdin is not interactive.\n");
            return -1;
        }
        password = passbuf;
    }
    options.ssid = ssid;
    options.password = password;
    options.connection_name = name;
    return wifi_mode_sta_connect(ifname, &options);
}

static int command_driver(const char *ifname, int argc, char **argv)
{
    const char *action;
    const char *module_dir;

    if (argc < 1) {
        errno = EINVAL;
        return -1;
    }
    action = argv[0];
    module_dir = get_option(argc - 1, argv + 1, "--module-dir");

    if (strcmp(action, "status") == 0)
        return driver_status(ifname, module_dir);
    if (strcmp(action, "load") == 0)
        return driver_load(ifname, module_dir);
    if (strcmp(action, "unload") == 0)
        return driver_unload(ifname);
    if (strcmp(action, "reload") == 0)
        return driver_reload(ifname, module_dir);

    errno = EINVAL;
    return -1;
}

int main(int argc, char **argv)
{
    const char *ifname;
    const char *cmd;
    char debug_dir[PATH_MAX];
    int ret = -1;

    if (argc < 3) {
        usage(argv[0]);
        return EXIT_FAILURE;
    }
    ifname = argv[1];
    cmd = argv[2];

    /* Driver load/status must also work while the interface is absent. */
    if (strcmp(cmd, "driver") == 0) {
        ret = command_driver(ifname, argc - 3, argv + 3);
        goto done;
    }

    if (!interface_exists(ifname)) {
        fprintf(stderr,
                "Interface '%s' does not exist. Load the driver first with:\n"
                "  sudo %s %s driver load [--module-dir DIR]\n",
                ifname, argv[0], ifname);
        return EXIT_FAILURE;
    }

    if (strcmp(cmd, "status") == 0) {
        ret = command_status(ifname);
    } else if (strcmp(cmd, "mode") == 0 && argc >= 4) {
        if (strcmp(argv[3], "ap") == 0)
            ret = command_mode_ap(ifname, argc - 4, argv + 4);
        else if (strcmp(argv[3], "sta") == 0)
            ret = wifi_mode_to_sta(ifname);
        else if (strcmp(argv[3], "idle") == 0)
            ret = wifi_mode_to_sta_idle(ifname);
        else
            errno = EINVAL;
    } else if (strcmp(cmd, "sta-connect") == 0) {
        ret = command_sta_connect(ifname, argc - 3, argv + 3);
    } else if (strcmp(cmd, "sta-idle") == 0 ||
               strcmp(cmd, "sta-disconnect") == 0) {
        ret = wifi_mode_to_sta_idle(ifname);
    } else if (strcmp(cmd, "ap-start") == 0) {
        struct ap_options opt = {
            .config_path = argc >= 4 ? argv[3] : "./hostapd-ath9k.conf",
            .ip_cidr = argc >= 5 ? argv[4] : "192.168.50.1/24"
        };
        ret = wifi_mode_to_ap(ifname, &opt);
    } else if (strcmp(cmd, "ap-stop") == 0 || strcmp(cmd, "sta-mode") == 0) {
        ret = wifi_mode_to_sta(ifname);
    } else {
        if (edca_resolve_debug_dir(ifname, debug_dir, sizeof(debug_dir)) < 0) {
            fprintf(stderr,
                    "Cannot resolve ath9k debugfs for %s: %s\n"
                    "The custom driver may not be loaded. Check with:\n"
                    "  sudo %s %s driver status\n",
                    ifname, strerror(errno), argv[0], ifname);
            return EXIT_FAILURE;
        }
        if (strcmp(cmd, "show") == 0 && argc == 3)
            ret = edca_show(debug_dir);
        else if (strcmp(cmd, "set") == 0 && argc == 7)
            ret = edca_set(debug_dir, argv[3], argv[4], argv[5], argv[6]);
        else if (strcmp(cmd, "hw") == 0 && (argc == 3 || argc == 4))
            ret = edca_hw(debug_dir, argc == 4 ? argv[3] : NULL);
        else
            errno = EINVAL;
    }

done:
    if (ret < 0) {
        if (errno == EINVAL)
            usage(argv[0]);
        fprintf(stderr, "%s failed: %s\n", cmd, strerror(errno));
        return EXIT_FAILURE;
    }
    return EXIT_SUCCESS;
}
