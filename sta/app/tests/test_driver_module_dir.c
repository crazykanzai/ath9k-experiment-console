#include "driver_ctrl.h"
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/utsname.h>

int main(void)
{
    struct utsname uts;
    char out[4096], expected[4096], path[4096];
    FILE *file;
    assert(uname(&uts) == 0);
    assert(unsetenv("ATH9K_MODULE_DIR") == 0);
    assert(driver_default_module_dir(out, sizeof(out)) == 0);
    snprintf(expected, sizeof(expected), "./modules/%s", uts.release);
    assert(strcmp(out, expected) == 0);
    snprintf(path, sizeof(path), "%s/%s", APSTASELECT_MODULE_ROOT, uts.release);
    assert(mkdir(path, 0755) == 0);
    snprintf(path, sizeof(path), "%s/%s/updates", APSTASELECT_MODULE_ROOT, uts.release);
    assert(mkdir(path, 0755) == 0);
    snprintf(expected, sizeof(expected), "%s/%s/updates/apstaselect", APSTASELECT_MODULE_ROOT, uts.release);
    assert(mkdir(expected, 0755) == 0);
    assert(driver_default_module_dir(out, sizeof(out)) == 0);
    assert(strncmp(out, "./modules/", 10) == 0); /* No installed manifest yet. */
    snprintf(path, sizeof(path), "%s/%s/updates/apstaselect/bundle.json", APSTASELECT_MODULE_ROOT, uts.release);
    file=fopen(path,"w");assert(file);fputs("{}\n",file);fclose(file);
    assert(driver_default_module_dir(out, sizeof(out)) == 0);
    assert(strcmp(out, expected) == 0);
    assert(setenv("ATH9K_MODULE_DIR", "/explicit/user/choice", 1) == 0);
    assert(driver_default_module_dir(out, sizeof(out)) == 0);
    assert(strcmp(out, "/explicit/user/choice") == 0);
    assert(driver_default_module_dir(out, 2) < 0 && errno == ENAMETOOLONG);
    puts("PASS: fallback, incomplete install, persistent directory, explicit override, buffer bounds");
    return 0;
}
