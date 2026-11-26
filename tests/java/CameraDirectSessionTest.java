// SPDX-License-Identifier: GPL-3.0-or-later

import java.net.InetSocketAddress;
import java.util.Arrays;
import java.util.HexFormat;

import micam.pppp.AddrBlock;
import micam.pppp.Frame;
import micam.pppp.Msg;
import micam.pppp.Uid;

final class CameraDirectSessionTest {
    public static void main(String[] args) {
        Uid uid = Uid.parse("TEST-12345-FIXTURE");
        InetSocketAddress local = new InetSocketAddress("192.0.2.3", 12345);
        byte[] request = CameraDirectSession.request(uid.encode(), local);
        Frame frame = Frame.parse(request, request.length);
        if (request.length != 40 || frame.type() != Msg.P2P_REQ || !uid.equals(Uid.decode(frame.payload(), 0))) {
            throw new AssertionError("Unexpected P2P request envelope");
        }
        byte[] address = Arrays.copyOfRange(frame.payload(), Uid.WIRE_LEN, frame.payload().length);
        if (!"00023930030200c00000000000000000".equals(HexFormat.of().formatHex(address))) {
            throw new AssertionError("Incorrect PPPP address byte order");
        }
        if (!local.equals(CameraDirectSession.advertisedPeer(address))
                || CameraDirectSession.advertisedPeer(new byte[8]) != null
                || CameraDirectSession.advertisedPeer(new AddrBlock("127.0.0.1", 12345).encode()) != null
                || CameraDirectSession.advertisedPeer(new AddrBlock("192.168.10.5", 12345).encode()) != null
                || CameraDirectSession.advertisedPeer(new AddrBlock("192.0.2.3", 0).encode()) != null) {
            throw new AssertionError("Unexpected PUNCH_TO validation result");
        }
    }
}
