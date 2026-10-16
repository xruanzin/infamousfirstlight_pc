/* Offline AppContent provider for the configured base-game profile.
 * No package/DLC mounting, downloads, entitlement or license emulation; add-on folders in
 * <user>/addcont/<title id> and BB_DLC_DIR are reported as installed (see addon_list). */
#define _GNU_SOURCE
#include "runtime.h"
#include <dirent.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#define INVALID_ID ((int32_t)0x805a1000)
#define NOT_LOADED ((int32_t)0x805a1001)
#define PARAMETER ((int32_t)0x80d90002)
#define BUSY ((int32_t)0x80d90003)
static unsigned refs, initialized, configured;
static uint32_t parameters[5];
static unsigned queries, lists;
void runtime_content_configure(const uint32_t values[5]) {
    if (refs || initialized || (values[0]!=1 && values[0]!=3)) {
        fputs("ERROR: invalid or late content profile\n",stderr); exit(1);
    }
    memcpy(parameters,values,sizeof(parameters)); configured=1;
}
static void require_provider(void) {
    if (!configured) { fputs("STOP: AppContent needs --content-profile\n",stderr); exit(21); }
}
/* System modules: every library this eboot imports is provided by the host
 * runtime, so loading one only tracks a reference count. AppContent (0xb4)
 * additionally requires the configured content profile. */
static unsigned module_refs[0x10000];
static ABI int32_t module_load(uint16_t id) {
    if (!id) return INVALID_ID;
    if (id==0xb4) require_provider();
    if (module_refs[id]==UINT32_MAX) { fputs("STOP: module reference overflow\n",stderr); exit(21); }
    ++module_refs[id];
    if (id==0xb4) { refs=module_refs[id]; printf("Runtime: AppContent host provider loaded; references=%u\n",refs); }
    else printf("Runtime: system module 0x%x loaded (host implementation)\n",id);
    return 0;
}
static ABI int32_t module_loaded(uint16_t id) {
    if (!id) return INVALID_ID;
    return module_refs[id] ? 0 : NOT_LOADED;
}
static ABI int32_t module_unload(uint16_t id) {
    int32_t result=module_loaded(id);
    if (result) return result;
    --module_refs[id];
    if (id==0xb4) { refs=module_refs[id]; if (!refs) initialized=0; }
    return 0;
}
static void require_loaded(void) {
    require_provider();
    if (!refs) { fputs("STOP: AppContent called before module load\n",stderr); exit(21); }
}
static ABI int32_t content_init(const unsigned char *init,unsigned char *boot) {
    require_loaded();
    if (!init || !boot) return PARAMETER;
    if (initialized) return BUSY;
    /* Only the reserved-zero init profile is implemented. */
    for (unsigned i=0;i<32;++i) if (init[i]) return PARAMETER;
    memset(boot,0,40); initialized=1;
    puts("Runtime: AppContent initialized; offline base-game profile, no mounted add-ons");
    return 0;
}
static void require_initialized(void) {
    require_loaded();
    if (!initialized) { fputs("STOP: AppContent query before initialization\n",stderr); exit(21); }
}
static ABI int32_t param_int(uint32_t id,int32_t *out) {
    require_initialized();
    if (id>4 || !out) return PARAMETER;
    memcpy(out,&parameters[id],4); ++queries;
    printf("Runtime: AppContent parameter %u = %d\n",id,*out);
    return 0;
}
/* SceAppContentAddcontInfo: entitlement label (16 chars + NUL), padding, download status. */
#define LABEL_SIZE 17
#define ADDON_INSTALLED 4u
#define MAX_ADDONS 64
typedef struct { char label[LABEL_SIZE]; char padding[3]; uint32_t status; } AddonInfo;
/* The add-on folders of one folder: each subfolder is named by its entitlement label (up to 16
 * characters), as dumped (SPEXPANSIONDLC03/sce_sys/...). Labels seen before are skipped. */
static void scan_labels(const char *folder,char labels[][LABEL_SIZE],uint32_t *count) {
    DIR *dir=opendir(folder);
    for (struct dirent *e; dir && (e=readdir(dir));) {
        char path[4096];
        struct stat entry;
        size_t n=strlen(e->d_name);
        if (e->d_name[0]=='.' || n>=LABEL_SIZE || *count>=MAX_ADDONS) continue;
        if (snprintf(path,sizeof(path),"%s/%s",folder,e->d_name)>=(int)sizeof(path) ||
            stat(path,&entry) || !S_ISDIR(entry.st_mode)) continue;
        int seen=0;
        for (uint32_t i=0;i<*count;++i) seen|=!strcmp(labels[i],e->d_name);
        if (!seen) memcpy(labels[(*count)++],e->d_name,n+1);
    }
    if (dir) closedir(dir);
}
/* Add-ons are the folders <user>/addcont/<title id>/<entitlement label> (shadPS4's layout) and
 * those of BB_DLC_DIR (the launcher's DLC folder: <label> folders), each reported as installed.
 * No add-on data is mounted: Bloodborne: The Old Hunters (SPEXPANSIONDLC03) needs only the
 * entitlement, its data ships with patch 1.09. Without a list the count is returned, with one
 * at most capacity entries are written. Never touch unused list slots. */
static ABI int32_t addon_list(uint32_t service,void *list,uint32_t capacity,uint32_t *hits) {
    require_initialized();
    if (service) { fputs("STOP: nonzero AppContent service label unsupported\n",stderr); exit(21); }
    if ((!capacity || !list) && !hits) return PARAMETER;
    static char labels[MAX_ADDONS][LABEL_SIZE];
    uint32_t found=0;
    char root[4096];
    int fits=snprintf(root,sizeof(root),"%s/addcont",runtime_file_user_dir())<(int)sizeof(root);
    DIR *titles=fits ? opendir(root) : NULL;
    for (struct dirent *t; titles && (t=readdir(titles));) {
        char title[4096];
        if (t->d_name[0]=='.' || snprintf(title,sizeof(title),"%s/%s",root,t->d_name)>=(int)sizeof(title)) continue;
        scan_labels(title,labels,&found);
    }
    if (titles) closedir(titles);
    const char *dlc=getenv("BB_DLC_DIR");
    if (dlc && *dlc) scan_labels(dlc,labels,&found);
    uint32_t count=list && capacity && found>capacity ? capacity : found;
    if (list && capacity) for (uint32_t i=0;i<count;++i) {
        AddonInfo info;
        memset(&info,0,sizeof(info)); memcpy(info.label,labels[i],strlen(labels[i]));
        info.status=ADDON_INSTALLED;
        memcpy((unsigned char *)list+(size_t)i*sizeof(info),&info,sizeof(info));
    }
    if (hits) *hits=count;
    static uint32_t reported=0;
    if (found!=reported) printf("Runtime: AppContent add-ons reported installed: %u\n",(unsigned)(reported=found));
    ++lists; return 0;
}
static ABI int32_t addon_info(uint32_t service,const char *label,AddonInfo *out) {
    require_initialized();
    if (service || !label || !out) return PARAMETER;
    if (!label[0] || strnlen(label,LABEL_SIZE)>=LABEL_SIZE) return PARAMETER;
    char labels[MAX_ADDONS][LABEL_SIZE];
    uint32_t found=0;
    char root[4096];
    int fits=snprintf(root,sizeof(root),"%s/addcont",runtime_file_user_dir())<(int)sizeof(root);
    DIR *titles=fits ? opendir(root) : NULL;
    for (struct dirent *t; titles && (t=readdir(titles));) {
        char title[4096];
        if (t->d_name[0]=='.' || snprintf(title,sizeof(title),"%s/%s",root,t->d_name)>=(int)sizeof(title)) continue;
        scan_labels(title,labels,&found);
    }
    if (titles) closedir(titles);
    const char *dlc=getenv("BB_DLC_DIR");
    if (dlc && *dlc) scan_labels(dlc,labels,&found);
    for (uint32_t i=0;i<found;++i) if (!strcmp(labels[i],label)) {
        memset(out,0,sizeof(*out));
        memcpy(out->label,labels[i],strlen(labels[i]));
        out->status=ADDON_INSTALLED;
        return 0;
    }
    return (int32_t)0x80D90007; /* ORBIS_APP_CONTENT_ERROR_DRM_NO_ENTITLEMENT */
}
static const RuntimeExport exports[]={
    {"sceSysmoduleLoadModule",module_load},
    {"sceSysmoduleIsLoaded",module_loaded},
    {"sceSysmoduleUnloadModule",module_unload},
    {"sceAppContentInitialize",content_init},
    {"sceAppContentAppParamGetInt",param_int},
    {"sceAppContentGetAddcontInfoList",addon_list},
    {"sceAppContentGetAddcontInfo",addon_info},
};
uintptr_t runtime_content_resolve(const char *name) { return RUNTIME_LOOKUP(exports,name); }
void runtime_content_report(void) {
    printf("Runtime: AppContent references=%u, initialized=%u, parameter queries=%u, list queries=%u\n",refs,initialized,queries,lists);
}
