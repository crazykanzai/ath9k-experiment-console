#ifndef ATH9K_EDCA_COMMON_H
#define ATH9K_EDCA_COMMON_H


#include <stdbool.h>
#include <stddef.h>
#include <sys/types.h>

#define APP_NAME "ath9k-edca"
#define OUTPUT_BUF_SIZE 8192
#define SMALL_BUF_SIZE 256

bool interface_exists(const char *ifname);
int path_join(char *out, size_t out_size, const char *dir, const char *name);
int read_text_file(const char *path, char *buf, size_t size);
int write_text_file(const char *path, const char *text);
int make_pidfile_path(const char *ifname, char *out, size_t out_size);
int validate_ipv4_cidr(const char *text);
void sleep_ms(unsigned int milliseconds);

#endif
