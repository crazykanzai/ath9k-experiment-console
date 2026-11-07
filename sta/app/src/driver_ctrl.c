#include "driver_ctrl.h"

#include "common.h"
#include "dhcp_ctrl.h"
#include "edca_ctrl.h"
#include "hostapd_ctrl.h"
#include "process.h"
#include "progress.h"
#include "wifi_nm.h"
#include "wifi_state.h"

#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <limits.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/utsname.h>
#include <unistd.h>

#ifndef APSTASELECT_MODULE_ROOT
#define APSTASELECT_MODULE_ROOT "/lib/modules"
#endif

static const char *const module_names[] = {
    "mac80211",
    "ath9k_hw",
    "ath9k_common",
    "ath9k"
};

static bool module_loaded(const char *name)
{
    char path[PATH_MAX];
    int n = snprintf(path, sizeof(path), "/sys/module/%s", name);

    return n > 0 && (size_t)n < sizeof(path) && access(path, F_OK) == 0;
}

static int check_mac80211_holders(void)
{
    DIR *dir = opendir("/sys/module/mac80211/holders");
    struct dirent *entry;

    if (!dir)
        return errno == ENOENT ? 0 : -1;
    while ((entry = readdir(dir)) != NULL) {
        if (entry->d_name[0] == '.' || strcmp(entry->d_name, "ath9k") == 0)
            continue;
        fprintf(stderr, "mac80211 is also used by %s; unload it separately before changing the 04 stack.\n",
                entry->d_name);
        closedir(dir);
        errno = EBUSY;
        return -1;
    }
    closedir(dir);
    return 0;
}

static int run_checked(char *const argv[], char *output, size_t output_size)
{
    int code = 0;

    if (process_run(argv, output, output_size, &code) < 0)
        return -1;
    if (code != 0) {
        errno = EIO;
        return -1;
    }
    return 0;
}

int driver_default_module_dir(char *out, size_t out_size)
{
    struct utsname uts;
    const char *env = getenv("ATH9K_MODULE_DIR");
    int n;

    if (env && env[0])
        n = snprintf(out, out_size, "%s", env);
    else {
        char installed[PATH_MAX];
        char manifest[PATH_MAX];
        if (uname(&uts) < 0)
            return -1;
        n = snprintf(installed, sizeof(installed), "%s/%s/updates/apstaselect",
                     APSTASELECT_MODULE_ROOT, uts.release);
        if (n < 0 || (size_t)n >= sizeof(installed) ||
            path_join(manifest, sizeof(manifest), installed, "bundle.json") < 0)
            return -1;
        if (access(manifest, R_OK) == 0)
            n = snprintf(out, out_size, "%s", installed);
        else
            n = snprintf(out, out_size, "./modules/%s", uts.release);
    }
    if (n < 0 || (size_t)n >= out_size) {
        errno = ENAMETOOLONG;
        return -1;
    }
    return 0;
}

static int module_path(const char *module_dir, const char *name,
                       char *out, size_t out_size)
{
    char filename[64];
    int n = snprintf(filename, sizeof(filename), "%s.ko", name);

    if (n < 0 || (size_t)n >= sizeof(filename)) {
        errno = ENAMETOOLONG;
        return -1;
    }
    return path_join(out, out_size, module_dir, filename);
}

static int validate_one_module(const char *path, const char *kernel_release,
                               char *detail, size_t detail_size)
{
    char output[OUTPUT_BUF_SIZE];
    int code = 0;
    char *argv[] = { "modinfo", "-F", "vermagic", (char *)path, NULL };
    size_t release_len = strlen(kernel_release);

    if (access(path, R_OK) < 0) {
        snprintf(detail, detail_size, "missing or unreadable: %s\n", path);
        return -1;
    }
    if (process_run(argv, output, sizeof(output), &code) < 0 || code != 0) {
        snprintf(detail, detail_size, "cannot inspect module: %s\n%.*s", path, 1024, output);
        errno = EINVAL;
        return -1;
    }
    if (strncmp(output, kernel_release, release_len) != 0 ||
        !isspace((unsigned char)output[release_len])) {
        snprintf(detail, detail_size,
                 "kernel mismatch for %s\n  running: %s\n  vermagic: %.*s",
                 path, kernel_release, 1024, output);
        errno = EINVAL;
        return -1;
    }
    return 0;
}

static int validate_module_set(const char *module_dir,
                               char *detail, size_t detail_size)
{
    struct utsname uts;
    size_t i;

    if (!module_dir || !module_dir[0]) {
        errno = EINVAL;
        return -1;
    }
    if (uname(&uts) < 0)
        return -1;

    for (i = 0; i < sizeof(module_names) / sizeof(module_names[0]); i++) {
        char path[PATH_MAX];
        if (module_path(module_dir, module_names[i], path, sizeof(path)) < 0 ||
            validate_one_module(path, uts.release, detail, detail_size) < 0)
            return -1;
    }
    {
        char checksums[PATH_MAX];
        if (path_join(checksums, sizeof(checksums), module_dir, "bundle.sha256") < 0)
            return -1;
        if (access(checksums, F_OK) == 0) {
            char *argv[] = { "sha256sum", "--check", checksums, NULL };
            if (run_checked(argv, detail, detail_size) < 0)
                return -1;
        }
    }
    return 0;
}

static int wait_interface(const char *ifname, bool should_exist,
                          unsigned int timeout_ms)
{
    unsigned int waited = 0;

    while (waited <= timeout_ms) {
        if (interface_exists(ifname) == should_exist)
            return 0;
        sleep_ms(200);
        waited += 200;
    }
    errno = ETIMEDOUT;
    return -1;
}

static int prepare_for_unload(const char *ifname, struct progress *p,
                              int first_step, int total_steps)
{
    char pidfile[PATH_MAX];
    char error[OUTPUT_BUF_SIZE];

    if (make_pidfile_path(ifname, pidfile, sizeof(pidfile)) < 0)
        return -1;

    progress_step(p, first_step, total_steps, "Stop project DHCP and hostapd");
    if (dhcp_stop(ifname, error, sizeof(error)) < 0) {
        fprintf(stderr, "%s", error);
        return -1;
    }
    if (hostapd_stop(pidfile, 3000) < 0)
        return -1;

    if (!interface_exists(ifname))
        return 0;

    progress_step(p, first_step + 1, total_steps, "Disconnect STA and release NetworkManager");
    (void)wifi_nm_disconnect(ifname, error, sizeof(error));
    if (wifi_nm_set_managed(ifname, false, error, sizeof(error)) < 0) {
        fprintf(stderr, "%s", error);
        return -1;
    }

    progress_step(p, first_step + 2, total_steps, "Bring interface down and clear addresses");
    if (wifi_link_set(ifname, false, error, sizeof(error)) < 0 ||
        wifi_addr_flush(ifname, error, sizeof(error)) < 0) {
        fprintf(stderr, "%s", error);
        return -1;
    }

    progress_step(p, first_step + 3, total_steps, "Interface is ready for driver removal");
    return 0;
}

static int unload_modules(struct progress *p, int first_step, int total_steps)
{
    static const char *const unload_order[] = {
        "ath9k",
        "ath9k_common",
        "ath9k_hw",
        "mac80211"
    };
    size_t i;
    char output[OUTPUT_BUF_SIZE];

    for (i = 0; i < sizeof(unload_order) / sizeof(unload_order[0]); i++) {
        char *argv[] = { "rmmod", (char *)unload_order[i], NULL };
        if (!module_loaded(unload_order[i]))
            continue;
        progress_step(p, first_step + (int)i, total_steps,
                      "Unload module %s", unload_order[i]);
        if (run_checked(argv, output, sizeof(output)) < 0) {
            progress_failure(p, "Failed to unload %s", unload_order[i]);
            if (output[0])
                fprintf(stderr, "%s", output);
            return -1;
        }
    }
    return 0;
}

static int load_modules(const char *module_dir, struct progress *p,
                        int first_step, int total_steps)
{
    char output[OUTPUT_BUF_SIZE];
    size_t i;
    char *cfg80211_argv[] = { "modprobe", "cfg80211", NULL };
    char *libarc4_argv[] = { "modprobe", "libarc4", NULL };
    char *modprobe_argv[] = { "modprobe", "ath", NULL };

    progress_step(p, first_step, total_steps, "Ensure stock cfg80211, libarc4 and ath prerequisites are available");
    if (run_checked(cfg80211_argv, output, sizeof(output)) < 0 ||
        run_checked(libarc4_argv, output, sizeof(output)) < 0) {
        progress_failure(p, "Failed to load mac80211 prerequisites");
        if (output[0])
            fprintf(stderr, "%s", output);
        return -1;
    }
    if (run_checked(modprobe_argv, output, sizeof(output)) < 0) {
        progress_failure(p, "Failed to load shared ath core");
        if (output[0])
            fprintf(stderr, "%s", output);
        return -1;
    }

    for (i = 0; i < sizeof(module_names) / sizeof(module_names[0]); i++) {
        char path[PATH_MAX];
        char *argv[] = { "insmod", path, NULL };

        if (module_path(module_dir, module_names[i], path, sizeof(path)) < 0)
            return -1;
        progress_step(p, first_step + 1 + (int)i, total_steps,
                      "Load custom module %s", module_names[i]);
        if (run_checked(argv, output, sizeof(output)) < 0) {
            progress_failure(p, "Failed to load %s", path);
            if (output[0])
                fprintf(stderr, "%s", output);
            return -1;
        }
    }
    return 0;
}

static int finish_as_sta_idle(const char *ifname, struct progress *p,
                              int step, int total_steps)
{
    char error[OUTPUT_BUF_SIZE];
    char pidfile[PATH_MAX];
    struct wifi_state state;

    progress_step(p, step, total_steps,
                  "Return the recreated interface to safe STA_IDLE");
    if (wait_interface(ifname, true, 5000) < 0)
        return -1;
    sleep_ms(500);
    if (wifi_nm_set_managed(ifname, true, error, sizeof(error)) < 0 ||
        wifi_link_set(ifname, true, error, sizeof(error)) < 0) {
        if (error[0])
            fprintf(stderr, "%s", error);
        return -1;
    }
    (void)wifi_nm_disconnect(ifname, error, sizeof(error));
    if (make_pidfile_path(ifname, pidfile, sizeof(pidfile)) < 0)
        return -1;
    sleep_ms(500);
    if (wifi_state_read(ifname, pidfile, &state) < 0 ||
        strcmp(state.iftype, "managed") != 0 ||
        state.hostapd_alive || state.state == WIFI_STATE_STA_CONNECTED) {
        errno = EIO;
        return -1;
    }
    return 0;
}

int driver_status(const char *ifname, const char *module_dir)
{
    struct utsname uts;
    char dir[PATH_MAX];
    char detail[OUTPUT_BUF_SIZE] = {0};
    char debug_dir[PATH_MAX];
    size_t i;

    if (uname(&uts) < 0)
        return -1;
    if (!module_dir) {
        if (driver_default_module_dir(dir, sizeof(dir)) < 0)
            return -1;
        module_dir = dir;
    }

    printf("Kernel release : %s\n", uts.release);
    printf("Module dir     : %s\n", module_dir);
    for (i = 0; i < sizeof(module_names) / sizeof(module_names[0]); i++)
        printf("%-14s: %s\n", module_names[i],
               module_loaded(module_names[i]) ? "loaded" : "not loaded");

    printf("Module set     : ");
    if (validate_module_set(module_dir, detail, sizeof(detail)) == 0)
        puts("ready and kernel-compatible");
    else {
        puts("unavailable");
        if (detail[0])
            printf("%s", detail);
    }

    printf("EDCA extension : ");
    if (interface_exists(ifname) &&
        edca_resolve_debug_dir(ifname, debug_dir, sizeof(debug_dir)) == 0)
        printf("available (%s)\n", debug_dir);
    else
        puts("unavailable");
    return 0;
}

int driver_unload(const char *ifname)
{
    struct progress p;

    progress_init(&p);
    if (geteuid() != 0) {
        progress_failure(&p, "Driver management requires root; run with sudo.");
        errno = EACCES;
        return -1;
    }
    if (check_mac80211_holders() < 0) {
        progress_failure(&p, "mac80211 has other active wireless users");
        return -1;
    }
    if (prepare_for_unload(ifname, &p, 1, 9) < 0) {
        progress_failure(&p, "Could not prepare interface for driver removal");
        return -1;
    }
    if (unload_modules(&p, 5, 9) < 0)
        return -1;
    if (wait_interface(ifname, false, 3000) < 0) {
        progress_failure(&p, "Modules were removed but interface %s still exists", ifname);
        return -1;
    }
    progress_success(&p, "Custom ath9k stack unloaded; %s is no longer present.", ifname);
    return 0;
}

int driver_load(const char *ifname, const char *module_dir)
{
    struct progress p;
    char dir[PATH_MAX];
    char detail[OUTPUT_BUF_SIZE] = {0};

    progress_init(&p);
    if (geteuid() != 0) {
        progress_failure(&p, "Driver management requires root; run with sudo.");
        errno = EACCES;
        return -1;
    }
    if (!module_dir) {
        if (driver_default_module_dir(dir, sizeof(dir)) < 0)
            return -1;
        module_dir = dir;
    }
    if (module_loaded("mac80211") || module_loaded("ath9k") || module_loaded("ath9k_common") ||
        module_loaded("ath9k_hw")) {
        progress_failure(&p,
                         "mac80211/ath9k is already loaded; use 'driver reload'.");
        errno = EBUSY;
        return -1;
    }

    progress_step(&p, 1, 7, "Validate custom modules for the running kernel");
    if (validate_module_set(module_dir, detail, sizeof(detail)) < 0) {
        progress_failure(&p, "Custom module set is not usable");
        if (detail[0])
            fprintf(stderr, "%s", detail);
        return -1;
    }
    if (load_modules(module_dir, &p, 2, 7) < 0)
        return -1;
    if (finish_as_sta_idle(ifname, &p, 7, 7) < 0) {
        progress_failure(&p,
                         "Driver loaded, but interface could not be verified as STA_IDLE");
        return -1;
    }
    progress_success(&p, "Custom ath9k loaded; %s is ready in STA_IDLE.", ifname);
    return 0;
}

int driver_reload(const char *ifname, const char *module_dir)
{
    struct progress p;
    char dir[PATH_MAX];
    char detail[OUTPUT_BUF_SIZE] = {0};

    progress_init(&p);
    if (geteuid() != 0) {
        progress_failure(&p, "Driver management requires root; run with sudo.");
        errno = EACCES;
        return -1;
    }
    if (!module_dir) {
        if (driver_default_module_dir(dir, sizeof(dir)) < 0)
            return -1;
        module_dir = dir;
    }

    progress_step(&p, 1, 15, "Validate custom modules before changing network state");
    if (validate_module_set(module_dir, detail, sizeof(detail)) < 0) {
        progress_failure(&p, "Custom module set is not usable");
        if (detail[0])
            fprintf(stderr, "%s", detail);
        return -1;
    }

    if (check_mac80211_holders() < 0) {
        progress_failure(&p, "mac80211 has other active wireless users");
        return -1;
    }

    if (prepare_for_unload(ifname, &p, 2, 15) < 0) {
        progress_failure(&p, "Could not prepare interface for driver reload");
        return -1;
    }
    if (unload_modules(&p, 6, 15) < 0)
        return -1;
    if (wait_interface(ifname, false, 3000) < 0) {
        progress_failure(&p, "Old driver was not fully removed");
        return -1;
    }
    if (load_modules(module_dir, &p, 10, 15) < 0)
        return -1;
    if (finish_as_sta_idle(ifname, &p, 15, 15) < 0) {
        progress_failure(&p,
                         "Custom driver loaded, but final STA_IDLE verification failed");
        return -1;
    }
    progress_success(&p,
                     "Custom ath9k reloaded; %s is ready in STA_IDLE.", ifname);
    return 0;
}
