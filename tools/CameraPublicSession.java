// SPDX-License-Identifier: GPL-3.0-or-later

import java.io.IOException;
import java.net.DatagramSocket;
import java.net.InetSocketAddress;
import java.time.Duration;
import java.util.List;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorCompletionService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.function.BooleanSupplier;
import java.util.function.Consumer;

import micam.pppp.RelaySession;
import micam.pppp.Uid;

/** Opens public transports, then transfers only the winning socket to MISS. */
final class CameraPublicSession implements AutoCloseable {
    private static final Duration PUNCH_TIMEOUT = Duration.ofSeconds(7);
    private static final Duration RELAY_TIMEOUT = Duration.ofSeconds(30);
    private static final Duration RACE_TIMEOUT = Duration.ofSeconds(34);
    private static final long SECOND_ROUTE_DELAY_MS = 750;

    record Peer(DatagramSocket socket, InetSocketAddress address, boolean direct) {}

    interface Candidate {
        Peer connect() throws Exception;
        void cancel();
    }

    private final Uid uid;
    private final List<String> servers;
    private RelaySession relay;
    private DatagramSocket direct;
    private boolean closed;

    CameraPublicSession(Uid uid, List<String> servers) {
        this.uid = uid;
        this.servers = List.copyOf(servers);
    }

    Peer connect(boolean relayOnly, boolean preferDirect, BooleanSupplier stopped, Consumer<String> report,
                 Consumer<String> relayProgress, Runnable relayFinished) throws IOException, InterruptedException {
        Candidate viaRelay = new Candidate() {
            public Peer connect() throws InterruptedException {
                if (preferDirect && !relayOnly) Thread.sleep(SECOND_ROUTE_DELAY_MS);
                report.accept("ROUTE START RELAY");
                RelaySession session = openRelay(relayProgress);
                try {
                    return new Peer(session.socket(), session.connect(RELAY_TIMEOUT).relay(), false);
                } finally {
                    relayFinished.run();
                }
            }

            public void cancel() { closeRelay(); }
        };
        if (relayOnly) return race(List.of(viaRelay), stopped);
        Candidate viaDirect = new Candidate() {
            public Peer connect() throws IOException, InterruptedException {
                if (!preferDirect) Thread.sleep(SECOND_ROUTE_DELAY_MS);
                report.accept("ROUTE START P2P");
                DatagramSocket socket = openDirect();
                InetSocketAddress peer = CameraDirectSession.connect(socket, uid, servers, PUNCH_TIMEOUT,
                        () -> stopped.getAsBoolean() || socket.isClosed(), report);
                return peer == null ? null : new Peer(socket, peer, true);
            }

            public void cancel() { closeDirect(); }
        };
        return race(List.of(viaRelay, viaDirect), stopped);
    }

    private synchronized RelaySession openRelay(Consumer<String> progress) throws InterruptedException {
        checkOpen();
        return relay = new RelaySession(uid, servers, progress);
    }

    private synchronized DatagramSocket openDirect() throws IOException, InterruptedException {
        checkOpen();
        return direct = new DatagramSocket();
    }

    private void checkOpen() throws InterruptedException {
        if (closed || Thread.currentThread().isInterrupted()) throw new InterruptedException();
    }

    private synchronized void closeRelay() {
        if (relay != null) relay.close();
    }

    private synchronized void closeDirect() {
        if (direct != null) direct.close();
    }

    static Peer race(List<Candidate> candidates, BooleanSupplier stopped) throws IOException, InterruptedException {
        try (var workers = Executors.newVirtualThreadPerTaskExecutor()) {
            var completed = new ExecutorCompletionService<Peer>(workers);
            var pending = candidates.stream().map(candidate -> completed.submit(candidate::connect)).toList();
            int winner = -1;
            try {
                long deadline = System.nanoTime() + RACE_TIMEOUT.toNanos();
                int remaining = pending.size();
                while (remaining > 0 && !stopped.getAsBoolean() && System.nanoTime() < deadline) {
                    var result = completed.poll(100, TimeUnit.MILLISECONDS);
                    if (result == null) continue;
                    remaining--;
                    try {
                        Peer peer = result.get();
                        if (peer != null && !stopped.getAsBoolean()) {
                            winner = pending.indexOf(result);
                            return peer;
                        }
                    } catch (ExecutionException unavailable) {
                        // Failed candidates do not stop the remaining transport. Exceptions can contain endpoints.
                    }
                }
                throw new IOException("No public camera transport became ready");
            } finally {
                for (int index = 0; index < candidates.size(); index++) {
                    if (index == winner) continue;
                    pending.get(index).cancel(true);
                    candidates.get(index).cancel();
                }
            }
        }
    }

    @Override
    public synchronized void close() {
        closed = true;
        closeDirect();
        closeRelay();
    }
}
