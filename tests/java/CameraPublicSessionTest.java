// SPDX-License-Identifier: GPL-3.0-or-later

import java.io.IOException;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

final class CameraPublicSessionTest {
    private record Candidate(DatagramSocket socket, boolean direct, CountDownLatch started,
                             boolean completes, boolean fails) implements CameraPublicSession.Candidate {
        @Override
        public CameraPublicSession.Peer connect() throws Exception {
            started.countDown();
            if (!started.await(2, TimeUnit.SECONDS)) throw new AssertionError("Candidates did not overlap");
            if (fails) throw new IOException("Simulated transport failure");
            if (!completes) new CountDownLatch(1).await();
            return new CameraPublicSession.Peer(socket, new InetSocketAddress(InetAddress.getLoopbackAddress(), 32100), direct);
        }

        @Override
        public void cancel() { socket.close(); }
    }

    public static void main(String[] args) throws Exception {
        selectWinner(false, false);
        selectWinner(true, false);
        selectWinner(false, true);
        failedRaceClosesAllSockets();
    }

    private static void selectWinner(boolean directWins, boolean loserFails) throws Exception {
        var started = new CountDownLatch(2);
        try (var winner = new DatagramSocket(0, InetAddress.getLoopbackAddress());
             var loser = new DatagramSocket(0, InetAddress.getLoopbackAddress())) {
            var successful = new Candidate(winner, directWins, started, true, false);
            var unavailable = new Candidate(loser, !directWins, started, false, loserFails);
            var peer = CameraPublicSession.race(List.of(unavailable, successful), () -> false);
            if (peer.socket() != winner || peer.direct() != directWins || winner.isClosed() || !loser.isClosed()) {
                throw new AssertionError("The race must transfer the winner and close the losing socket");
            }
        }
    }

    private static void failedRaceClosesAllSockets() throws Exception {
        var started = new CountDownLatch(2);
        try (var relay = new DatagramSocket(0, InetAddress.getLoopbackAddress());
             var direct = new DatagramSocket(0, InetAddress.getLoopbackAddress())) {
            var candidates = List.<CameraPublicSession.Candidate>of(
                    new Candidate(relay, false, started, false, true),
                    new Candidate(direct, true, started, false, true));
            try {
                CameraPublicSession.race(candidates, () -> false);
                throw new AssertionError("A failed race cannot return a socket");
            } catch (IOException expected) {
                if (!relay.isClosed() || !direct.isClosed()) throw new AssertionError("Failed sockets must be closed");
            }
        }
    }
}
