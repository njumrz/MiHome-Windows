// SPDX-License-Identifier: GPL-3.0-or-later

import micam.miss.MediaPacket;

final class CameraPairedMediaTest {
    public static void main(String[] args) {
        checkLens(0x001E2000, false, 1);
        checkLens(0x011E2001, false, 1);
        checkLens(0x015E2000, false, 2);
        checkLens(0x005E2001, false, 2);
        checkLens(0x001E2000, true, 1);
        checkLens(0x015E2000, true, 2);
        checkLens(0x005E2001, true, 0);
    }

    private static void checkLens(int flags, boolean channelIndex, int expected) {
        MediaPacket packet = new MediaPacket(MediaPacket.CODEC_H265, 0, flags, 0, new byte[0]);
        if (CameraPairedMedia.lensOf(packet, channelIndex) != expected) {
            throw new AssertionError("Video packet was sent to the wrong lens");
        }
    }
}
