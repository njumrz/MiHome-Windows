// SPDX-License-Identifier: GPL-3.0-or-later

import java.io.IOException;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.SocketTimeoutException;
import java.time.Duration;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.function.BooleanSupplier;
import java.util.function.Consumer;

import micam.pppp.AddrBlock;
import micam.pppp.Frame;
import micam.pppp.Msg;
import micam.pppp.Uid;

/** Attempts a public IPv4 PPPP connection using the directory from this camera's cloud grant. */
final class CameraDirectSession {
    private static final int[] DIRECTORY_PORTS = {32101, 32102, 32100};
    private static final int MAX_PEERS = 12;

    static InetSocketAddress connect(DatagramSocket socket, Uid uid, List<String> servers,
                                     Duration timeout, BooleanSupplier stopped, Consumer<String> report) throws IOException {
        byte[] identity = uid.encode();
        byte[] punch = Frame.encode(Msg.PUNCH_PKT, identity);
        Map<InetAddress, byte[]> requests = new LinkedHashMap<>();
        for (String server : servers) {
            try {
                InetAddress address = InetAddress.getByName(server);
                if (!publicIpv4(address)) continue;
                // Ask the OS which IPv4 address it would use, then advertise the real session socket's port.
                try (DatagramSocket route = new DatagramSocket()) {
                    route.connect(address, DIRECTORY_PORTS[0]);
                    if (!(route.getLocalAddress() instanceof Inet4Address local)) continue;
                    requests.put(address, request(identity, new InetSocketAddress(local, socket.getLocalPort())));
                }
            } catch (IOException ignored) {
                // A failed directory entry should not prevent trying other entries from the grant.
            }
        }

        Set<InetSocketAddress> candidates = new LinkedHashSet<>();
        socket.setSoTimeout(200);
        long start = System.nanoTime();
        long deadline = start + timeout.toNanos();
        long discoverAt = start;
        long punchAt = start;
        int sent = 0, acknowledged = 0, offers = 0, punches = 0;
        byte[] buffer = new byte[2048];
        try {
            while (!requests.isEmpty() && !stopped.getAsBoolean() && System.nanoTime() < deadline) {
                long now = System.nanoTime();
                if (now >= discoverAt) {
                    for (var entry : requests.entrySet()) {
                        for (int port : DIRECTORY_PORTS) {
                            send(socket, new InetSocketAddress(entry.getKey(), port), entry.getValue());
                            sent++;
                        }
                    }
                    discoverAt = now + Duration.ofMillis(1600).toNanos();
                }
                if (now >= punchAt) {
                    for (InetSocketAddress candidate : candidates) {
                        send(socket, candidate, punch);
                        punches++;
                    }
                    punchAt = now + Duration.ofMillis(500).toNanos();
                }

                DatagramPacket packet = new DatagramPacket(buffer, buffer.length);
                try {
                    socket.receive(packet);
                } catch (SocketTimeoutException ignored) {
                    continue;
                }
                int length = packet.getLength();
                if (length < Frame.HEADER_LEN || (((buffer[2] & 255) << 8) | (buffer[3] & 255)) != length - Frame.HEADER_LEN) continue;
                Frame frame = Frame.parse(buffer, length);
                if (frame == null) continue;
                InetSocketAddress source = new InetSocketAddress(packet.getAddress(), packet.getPort());
                boolean directory = requests.containsKey(source.getAddress());
                if (directory && frame.type() == Msg.P2P_REQ_ACK) acknowledged++;
                if (directory && frame.type() == Msg.PUNCH_TO) {
                    InetSocketAddress candidate = advertisedPeer(frame.payload());
                    if (candidate != null && candidates.size() < MAX_PEERS && candidates.add(candidate)) {
                        offers++;
                        send(socket, candidate, punch);
                        punches++;
                    }
                }
                if (frame.type() == Msg.PUNCH_PKT && publicIpv4(source.getAddress())
                        && Arrays.equals(frame.payload(), identity) && candidates.size() < MAX_PEERS && candidates.add(source)) {
                    send(socket, source, punch);
                    punches++;
                }
                if (frame.type() == Msg.P2P_RDY && publicIpv4(source.getAddress())
                        && (Arrays.equals(frame.payload(), identity) || (frame.payload().length == 0
                        && candidates.stream().anyMatch(candidate -> candidate.getAddress().equals(source.getAddress()))))) {
                    // The final UDP port can differ from the port that was nominated for hole punching.
                    socket.connect(source);
                    return source;
                }
            }
            return null;
        } finally {
            report.accept("DIRECT STATS %d %d %d %d %d".formatted(sent, acknowledged, offers, punches,
                    Duration.ofNanos(System.nanoTime() - start).toMillis()));
        }
    }

    static byte[] request(byte[] identity, InetSocketAddress local) {
        byte[] payload = Arrays.copyOf(identity, Uid.WIRE_LEN + AddrBlock.WIRE_LEN);
        System.arraycopy(AddrBlock.of(local).encode(), 0, payload, Uid.WIRE_LEN, AddrBlock.WIRE_LEN);
        return Frame.encode(Msg.P2P_REQ, payload);
    }

    static InetSocketAddress advertisedPeer(byte[] payload) {
        if (payload.length < AddrBlock.WIRE_LEN || payload[0] != 0 || payload[1] != 2) return null;
        InetSocketAddress address = AddrBlock.decode(payload, 0).toSocketAddress();
        return address.getPort() > 0 && publicIpv4(address.getAddress()) ? address : null;
    }

    private static boolean publicIpv4(InetAddress address) {
        if (!(address instanceof Inet4Address) || address.isAnyLocalAddress() || address.isLoopbackAddress()
                || address.isLinkLocalAddress() || address.isSiteLocalAddress() || address.isMulticastAddress()) return false;
        byte[] octets = address.getAddress();
        int first = octets[0] & 255, second = octets[1] & 255;
        return first > 0 && first < 224 && !(first == 100 && second >= 64 && second <= 127);
    }

    private static void send(DatagramSocket socket, InetSocketAddress address, byte[] payload) throws IOException {
        socket.send(new DatagramPacket(payload, payload.length, address));
    }
}
