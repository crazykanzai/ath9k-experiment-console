#ifndef ATH9K_EDCA_CTRL_H
#define ATH9K_EDCA_CTRL_H

#include <stddef.h>

int edca_resolve_debug_dir(const char *ifname, char *out, size_t out_size);
int edca_show(const char *debug_dir);
int edca_set(const char *debug_dir, const char *ac_name,
             const char *aifs_text, const char *cwmin_text,
             const char *cwmax_text);
int edca_hw(const char *debug_dir, const char *selected_ac);

#endif
