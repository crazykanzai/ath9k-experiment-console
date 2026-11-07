#ifndef ATH9K_EDCA_PROMPT_H
#define ATH9K_EDCA_PROMPT_H

#include <stddef.h>

int prompt_text(const char *label, const char *default_value,
                char *out, size_t out_size);
int prompt_secret(const char *label, char *out, size_t out_size);

#endif
