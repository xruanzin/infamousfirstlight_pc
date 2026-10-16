/* First Light development hotfix layer.
 *
 * This file deliberately keeps title iteration out of the monolithic loader:
 *  - NID -> symbol aliases are read from an external TSV file at runtime.
 *  - On Windows an optional hotfix DLL can override/add ABI implementations.
 *
 * The portable launcher sets FL_IMPORT_ALIASES and FL_RUNTIME_MODULE to files
 * inside its own runtime\ directory, so text-only fixes can be dropped into an
 * existing install without rebuilding bb-probe.exe.
 */
#define _GNU_SOURCE
#include "runtime.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#include <windows.h>
#endif

#define HOTFIX_MAX_ALIASES 512
#define HOTFIX_NID_MAX 96
#define HOTFIX_SYMBOL_MAX 128

typedef struct {
    char nid[HOTFIX_NID_MAX];
    char symbol[HOTFIX_SYMBOL_MAX];
} HotfixAlias;

static HotfixAlias aliases[HOTFIX_MAX_ALIASES];
static size_t alias_count;
static int aliases_loaded;

#ifdef _WIN32
typedef uintptr_t (*HotfixResolveV1)(const char *nid, const char *symbol);
typedef void (*HotfixReportV1)(void);
static HMODULE module_handle;
static HotfixResolveV1 module_resolve;
static HotfixReportV1 module_report;
static int module_checked;
#endif

static int same_stable_nid(const char *a,const char *b) {
    const char *as=a ? strchr(a,'#') : NULL;
    const char *bs=b ? strchr(b,'#') : NULL;
    size_t an=a ? (as ? (size_t)(as-a) : strlen(a)) : 0;
    size_t bn=b ? (bs ? (size_t)(bs-b) : strlen(b)) : 0;
    return a && b && an==bn && !memcmp(a,b,an);
}

static void load_aliases(void) {
    if (aliases_loaded) return;
    aliases_loaded=1;
    const char *path=getenv("FL_IMPORT_ALIASES");
    if (!path || !*path) return;
    FILE *f=fopen(path,"rb");
    if (!f) {
        fprintf(stderr,"Runtime hotfix: aliases not found: %s\n",path);
        return;
    }
    char line[512];
    while (alias_count<HOTFIX_MAX_ALIASES && fgets(line,sizeof(line),f)) {
        char *p=line;
        while (*p==' ' || *p=='\t') ++p;
        if (!*p || *p=='#' || *p=='\r' || *p=='\n') continue;
        char *tab=strchr(p,'\t');
        if (!tab) continue;
        *tab++=0;
        char *end=strpbrk(tab,"\r\n");
        if (end) *end=0;
        if (!*p || !*tab || strlen(p)>=HOTFIX_NID_MAX || strlen(tab)>=HOTFIX_SYMBOL_MAX) continue;
        snprintf(aliases[alias_count].nid,sizeof(aliases[alias_count].nid),"%s",p);
        snprintf(aliases[alias_count].symbol,sizeof(aliases[alias_count].symbol),"%s",tab);
        ++alias_count;
    }
    fclose(f);
    printf("Runtime hotfix: loaded %zu external import alias(es) from %s\n",alias_count,path);
}

const char *runtime_hotfix_symbol(const char *nid) {
    load_aliases();
    for (size_t i=0;i<alias_count;++i)
        if (!strcmp(nid,aliases[i].nid)) return aliases[i].symbol;
    for (size_t i=0;i<alias_count;++i)
        if (same_stable_nid(nid,aliases[i].nid)) return aliases[i].symbol;
    return NULL;
}

#ifdef _WIN32
static void load_module(void) {
    if (module_checked) return;
    module_checked=1;
    const char *path=getenv("FL_RUNTIME_MODULE");
    if (!path || !*path) return;
    DWORD attrs=GetFileAttributesA(path);
    if (attrs==INVALID_FILE_ATTRIBUTES || (attrs&FILE_ATTRIBUTE_DIRECTORY)) return;
    module_handle=LoadLibraryA(path);
    if (!module_handle) {
        fprintf(stderr,"Runtime hotfix: failed loading module %s (Win32 error %lu)\n",
                path,(unsigned long)GetLastError());
        return;
    }
    module_resolve=(HotfixResolveV1)(void *)GetProcAddress(module_handle,"fl_runtime_resolve_v1");
    module_report=(HotfixReportV1)(void *)GetProcAddress(module_handle,"fl_runtime_report_v1");
    if (!module_resolve) {
        fprintf(stderr,"Runtime hotfix: %s has no fl_runtime_resolve_v1 export\n",path);
        FreeLibrary(module_handle); module_handle=NULL; module_report=NULL;
        return;
    }
    printf("Runtime hotfix: loaded native module %s\n",path);
}
#endif

uintptr_t runtime_hotfix_resolve(const char *nid,const char *symbol) {
#ifdef _WIN32
    load_module();
    if (module_resolve) return module_resolve(nid,symbol);
#else
    (void)nid; (void)symbol;
#endif
    return 0;
}

void runtime_hotfix_report(void) {
    load_aliases();
#ifdef _WIN32
    load_module();
    printf("Runtime: hotfix aliases=%zu, native module=%s\n",
           alias_count,module_resolve ? "loaded" : "none");
    if (module_report) module_report();
#else
    printf("Runtime: hotfix aliases=%zu\n",alias_count);
#endif
}
