#ifndef ATH9K_EDCA_DRIVER_CTRL_H
#define ATH9K_EDCA_DRIVER_CTRL_H

#include <stddef.h>

int driver_default_module_dir(char *out, size_t out_size);
int driver_status(const char *ifname, const char *module_dir);
int driver_load(const char *ifname, const char *module_dir);
int driver_unload(const char *ifname);
int driver_reload(const char *ifname, const char *module_dir);

#endif
