# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the optional dual-lens adapter from a local MijiaDeck v1.1.2 checkout."""

import argparse
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile

_CLASS_NAME = "CameraRelayDualLens"
_JAR_NAME = "camera-relay-dual-lens-v4.jar"
_CHECK = "CAMERA_RELAY_DUAL_LENS"


def build(jdk: Path, source: Path, relay_dir: Path) -> Path:
    relay_dir = relay_dir.resolve()
    upstream = relay_dir / "camera-relay.jar"
    runtime = relay_dir / "runtime/bin/java.exe"

    java = source.read_text(encoding="utf-8")
    # Match go2rtc's MISS StartMedia request for channel=2. Handle lens changes on the
    # media-reading thread so the previous stream cannot publish into the new HTTP endpoint.
    java = java.replace("CameraRelay", _CLASS_NAME).replace("CAMERA_RELAY_2", _CHECK)
    start_call = "camera.startVideo(quality, audio);"
    if java.count(start_call) != 3 or java.count(_CHECK) != 1:
        raise ValueError("Expected the CameraRelay.java source from MijiaDeck v1.1.2")
    java = java.replace(start_call, "startLens();")
    replacements = {
        "    private static int videoCodec;": r'''    private static int videoCodec;
    private static volatile int lens = 1;
    private static volatile String[] nextStream;
    private static String relayStage;
    private static long videoBytes;
    private static long lastByteCount;

    private static void reportVideoBytes(MediaPacket packet) {
        videoBytes += packet.payload().length;
        long now = System.nanoTime();
        if (now - lastByteCount >= Duration.ofSeconds(1).toNanos()) {
            report("BYTES " + videoBytes);
            lastByteCount = now;
        }
    }

    private static void relayProgress(String message) {
        String stage;
        // The upstream trace contains credentials. Emit only these fixed stage names.
        if (message.startsWith("uid ") || message.startsWith("retrying discovery")) stage = "DISCOVERY";
        else if (message.startsWith("  candidate ")) stage = "REGISTER";
        else if (message.startsWith("relay selected ")) stage = "PORT";
        else if (message.startsWith("relay session ")) stage = "CAMERA";
        else return;
        if (!stage.equals(relayStage)) {
            relayStage = stage;
            report("RELAY " + stage);
        }
    }

    private static void startLens() {
        if (lens == 1) camera.startVideo(quality, audio);
        else camera.sendEncrypted(MissClient.CMD_VIDEO_START,
                "{\"videoquality\":-1,\"videoquality2\":%d,\"enableaudio\":%d}"
                .formatted(quality, audio ? 1 : 0));
    }

    private static MediaPacket nextKeyframe() {
        long deadline = System.nanoTime() + Duration.ofSeconds(20).toNanos();
        while (!stopped.get() && System.nanoTime() < deadline) {
            MediaPacket packet = camera.nextMedia(Duration.ofMillis(500));
            if (packet != null && packet.isVideo() && packet.isKeyframe()) return packet;
        }
        throw new IllegalStateException();
    }

    private static void pauseVideo() {
        camera.stopVideo();
        long deadline = System.nanoTime() + Duration.ofSeconds(2).toNanos();
        while (camera.nextMedia(Duration.ofMillis(350)) != null) {
            if (stopped.get() || System.nanoTime() >= deadline) throw new IllegalStateException();
        }
    }

    private static void switchStream(String[] fields) throws Exception {
        server.close();
        pauseVideo();
        if (stopped.get()) return;
        boolean changeQuality = fields[0].equals("QUALITY");
        int previousQuality = quality;
        int value = Integer.parseInt(fields[1]);
        if (changeQuality) quality = value;
        else lens = value;
        MediaPacket first;
        try {
            startLens();
            first = nextKeyframe();
            if (changeQuality) report("QUALITY ACCEPTED");
        } catch (RuntimeException error) {
            if (!changeQuality || stopped.get()) throw error;
            pauseVideo();
            quality = previousQuality;
            startLens();
            first = nextKeyframe();
            report("QUALITY REJECTED");
        }
        server = new HttpStreamServer(Integer.parseInt(fields[2]), fields[3], null);
        server.start();
        server.publish(first);
    }
''',
        '        String phase = "CONFIG";': '''        String phase = "CONFIG";
        if (args.length == 1 && args[0].equals("2")) lens = 2;''',
        'new RelaySession(Uid.parse(uid), InitString.servers(init), null)':
            'new RelaySession(Uid.parse(uid), InitString.servers(init), CameraRelayDualLens::relayProgress)',
        '                    server.publish(packet);':
            '                    server.publish(packet);\n                    reportVideoBytes(packet);',
        '                            if (command.matches("QUALITY [012]")) {': r'''                            if (command.matches("(?:SWITCH [12]|QUALITY [012]) [0-9]{1,5} /camera-[a-f0-9]{32}\\.ts")) {
                                nextStream = command.split(" ");
                            } else if (command.matches("QUALITY [012]")) {''',
        '''            long deadline = System.nanoTime() + Duration.ofSeconds(20).toNanos();
            MediaPacket first = null;
            while (!stopped.get() && System.nanoTime() < deadline) {
                MediaPacket packet = camera.nextMedia(Duration.ofSeconds(1));
                if (packet != null && packet.isVideo() && packet.isKeyframe()) {
                    first = packet;
                    break;
                }
            }
            if (first == null) throw new IllegalStateException();''': '            MediaPacket first = nextKeyframe();',
        '''            while (!stopped.get()) {
                MediaPacket packet = camera.nextMedia(Duration.ofSeconds(1));''': '''            while (!stopped.get()) {
                String[] requested = nextStream;
                if (requested != null) {
                    nextStream = null;
                    phase = requested[0];
                    switchStream(requested);
                    if (stopped.get()) return;
                    report("READY");
                    phase = "STREAM";
                    lastFrame = System.nanoTime();
                }
                MediaPacket packet = camera.nextMedia(Duration.ofMillis(500));''',
    }
    for original, replacement in replacements.items():
        if java.count(original) != 1:
            raise ValueError("Expected the CameraRelay.java source from MijiaDeck v1.1.2")
        java = java.replace(original, replacement)

    target = relay_dir / _JAR_NAME
    with TemporaryDirectory(prefix="relay-build-", dir=relay_dir) as build_dir:
        temp = Path(build_dir)
        java_file = temp / f"{_CLASS_NAME}.java"
        java_file.write_text(java, encoding="utf-8")
        subprocess.run([
            str(jdk.resolve() / "bin/javac.exe"), "--release", "21", "-encoding", "UTF-8",
            "-cp", str(upstream), "-d", str(temp), str(java_file),
        ], check=True)

        candidate = temp / _JAR_NAME
        with ZipFile(candidate, "w", ZIP_DEFLATED) as jar:
            for compiled in sorted(temp.glob("*.class")):
                jar.write(compiled, compiled.name)
        classpath = os.pathsep.join(map(str, (candidate, upstream)))
        result = subprocess.run(
            [str(runtime), "-cp", classpath, _CLASS_NAME, "--check"],
            capture_output=True, text=True, timeout=15, check=True,
        )
        if result.stdout.strip() != _CHECK:
            raise RuntimeError("The compiled dual-lens adapter did not pass its startup check")
        candidate.replace(target)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jdk", type=Path, required=True, help="Windows JDK 21 directory")
    parser.add_argument("--source", type=Path, required=True, help="MijiaDeck v1.1.2 CameraRelay.java")
    parser.add_argument("--relay-dir", type=Path, required=True, help="Local MijiaDeck relay component directory")
    args = parser.parse_args()
    print(build(args.jdk, args.source, args.relay_dir))
