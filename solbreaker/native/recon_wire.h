#ifndef TFS_RECON_WIRE_H
#define TFS_RECON_WIRE_H

#include <stddef.h>
#include <stdint.h>

#define RECON_MAX_IDS 49u
#define RECON_MAX_PAYLOAD 242u

struct recon_message {
    uint8_t battle_id[16];
    uint32_t sequence;
    uint32_t lamp;
    uint32_t count;
    uint32_t ids[RECON_MAX_IDS];
};

/* Returns 1 on success. On rejection, leaves out unchanged. */
int tfs_recon_decode(const uint8_t *data, size_t length,
                     const uint8_t expected_battle[16],
                     uint32_t previous_sequence, int has_previous,
                     struct recon_message *out);

#endif
