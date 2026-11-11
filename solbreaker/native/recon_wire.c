/* Bounded, platform-independent decoder for TFS recon wire v1.
 * This is a codec only: it does not hook or modify the game client.
 */
#include "recon_wire.h"
#include <string.h>

static uint32_t read32(const uint8_t *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static uint32_t crc32_bytes(const uint8_t *p, size_t length) {
    uint32_t crc = UINT32_C(0xffffffff);
    size_t i;
    for (i = 0; i < length; ++i) {
        unsigned bit;
        crc ^= p[i];
        for (bit = 0; bit < 8; ++bit)
            crc = (crc >> 1) ^ (UINT32_C(0xedb88320) & (uint32_t)-(int32_t)(crc & 1u));
    }
    return ~crc;
}

/* Return 1 on success, 0 on rejection. Never mutate out on rejection. */
int tfs_recon_decode(const uint8_t *data, size_t length,
                     const uint8_t expected_battle[16],
                     uint32_t previous_sequence, int has_previous,
                     struct recon_message *out) {
    struct recon_message message;
    uint32_t count, i, last = 0;
    int uuid_nonzero = 0;
    if (!data || !expected_battle || !out || length < 46 || length > RECON_MAX_PAYLOAD ||
        (has_previous != 0 && has_previous != 1)) return 0;
    if (data[0] < 11 || data[0] > 60 || length != 2u + (size_t)data[0] * 4u ||
        data[length - 1] != 0) return 0;
    if (read32(data + 1) != UINT32_C(0x31534654) ||
        read32(data + 5) != UINT32_C(0x4e4f4345) || read32(data + 9) != 1u) return 0;
    if (crc32_bytes(data + 1, length - 6) != read32(data + length - 5)) return 0;
    for (i = 0; i < 16; ++i) uuid_nonzero |= data[13 + i];
    if (!uuid_nonzero || memcmp(data + 13, expected_battle, 16) != 0) return 0;
    memset(&message, 0, sizeof(message));
    memcpy(message.battle_id, data + 13, 16);
    message.sequence = read32(data + 29);
    if (has_previous && message.sequence <= previous_sequence) return 0;
    message.lamp = read32(data + 33);
    count = read32(data + 37);
    if (message.lamp > 1 || count > RECON_MAX_IDS || count != (uint32_t)data[0] - 11u) return 0;
    message.count = count;
    for (i = 0; i < count; ++i) {
        uint32_t current = read32(data + 41 + (size_t)i * 4u);
        if (current == 0 || current <= last) return 0;
        message.ids[i] = last = current;
    }
    *out = message;
    return 1;
}
