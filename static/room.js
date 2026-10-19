/*
 * Video hearing room (WebRTC, up to about 6 people).
 *
 * How it works:
 *  1. Each browser tab gets a random id and posts a "join" message to the server.
 *  2. People who were already in the room see the "join" and send the newcomer an "offer".
 *  3. The newcomer replies with an "answer". Both sides swap "candidate" messages to find a route.
 *  4. Video and audio then flow directly between browsers.
 * The server only stores and forwards these small messages (see the /signal and /signals routes).
 */
(function () {
    "use strict";

    const cfg = window.ROOM;
    const clientId = Math.random().toString(36).slice(2, 10);

    const peers = {};          // other people: id -> { id, name, pc, tile, queue, outbox, ready }
    let localStream = null;
    let lastSignalId = 0;
    let running = true;

    // ---- page elements ----
    const grid = document.getElementById("grid");
    const waiting = document.getElementById("waiting");
    const notice = document.getElementById("notice");
    const peopleList = document.getElementById("people");
    const chatLog = document.getElementById("chat-log");

    const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
    const initials = name => name.split(" ").slice(0, 2).map(w => w[0]).join("").toUpperCase();

    function showNotice(text) {
        notice.textContent = text;
        notice.classList.remove("hide");
    }

    // ---- talking to the server ----
    async function send(type, data, to) {
        const response = await fetch(cfg.signalUrl, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ type: type, data: data || {}, from: clientId, to: to || "" }),
        });
        if (!response.ok) throw new Error("Server said " + response.status);
        return response.json();
    }

    // ---- video tiles and the people list ----
    function makeTile(id, name, isLocal) {
        const tile = document.createElement("div");
        tile.className = "tile" + (isLocal ? " local" : "");
        tile.id = "tile-" + id;
        tile.innerHTML = '<div class="tile-avatar"></div><video autoplay playsinline></video>' +
                         '<span class="tile-name"></span><span class="tile-state"></span>';
        tile.querySelector(".tile-avatar").textContent = initials(name);
        tile.querySelector(".tile-name").textContent = isLocal ? name + " (You)" : name;
        tile.querySelector("video").muted = isLocal;       // never play your own voice back
        grid.appendChild(tile);
        return tile;
    }

    function refreshRoom() {
        const count = grid.children.length;
        grid.dataset.count = count;
        waiting.classList.toggle("hide", Object.keys(peers).length > 0);

        const everyone = [{ name: cfg.myName + " (You)" }].concat(Object.values(peers));
        peopleList.innerHTML = "";
        everyone.forEach(function (person) {
            const item = document.createElement("li");
            item.innerHTML = '<span class="avatar"></span><span></span>';
            item.children[0].textContent = initials(person.name);
            item.children[1].textContent = person.name;
            peopleList.appendChild(item);
        });
    }

    function setTileState(peer, text) {
        const label = peer.tile.querySelector(".tile-state");
        label.textContent = text;
        label.classList.toggle("show", Boolean(text));
    }

    // ---- connections to other people ----
    function createPeer(id, name) {
        removePeer(id);                                    // same person rejoining? start fresh

        const pc = new RTCPeerConnection({ iceServers: cfg.iceServers });
        const peer = { id: id, name: name, pc: pc, tile: makeTile(id, name, false), queue: [], outbox: [], ready: false };
        peers[id] = peer;

        if (localStream) {
            localStream.getTracks().forEach(track => pc.addTrack(track, localStream));
        } else {                                           // no camera or mic: still receive
            pc.addTransceiver("audio", { direction: "recvonly" });
            pc.addTransceiver("video", { direction: "recvonly" });
        }

        pc.onicecandidate = function (event) {
            if (!event.candidate) return;
            // Hold candidates back until our offer/answer has reached the server, so they arrive in order.
            if (peer.ready) send("candidate", event.candidate.toJSON(), id).catch(() => {});
            else peer.outbox.push(event.candidate.toJSON());
        };
        pc.ontrack = function (event) {
            const video = peer.tile.querySelector("video");
            if (video.srcObject !== event.streams[0]) video.srcObject = event.streams[0];
        };
        pc.onconnectionstatechange = function () {
            const state = pc.connectionState;
            if (state === "connected") setTileState(peer, "");
            else if (state === "failed" || state === "disconnected") setTileState(peer, "Connection problem");
            else if (state === "connecting") setTileState(peer, "Connecting…");
        };

        refreshRoom();
        return peer;
    }

    function removePeer(id) {
        const peer = peers[id];
        if (!peer) return;
        peer.pc.close();
        peer.tile.remove();
        delete peers[id];
        refreshRoom();
    }

    async function markReady(peer) {                       // offer/answer delivered: release held candidates
        peer.ready = true;
        for (const candidate of peer.outbox) await send("candidate", candidate, peer.id).catch(() => {});
        peer.outbox = [];
    }

    async function applyQueuedCandidates(peer) {
        for (const candidate of peer.queue) await peer.pc.addIceCandidate(candidate).catch(() => {});
        peer.queue = [];
    }

    // ---- handling messages from the server ----
    async function handle(message) {
        const from = message.from;

        if (message.type === "join") {                     // someone new: we call them
            const peer = createPeer(from, message.name);
            await peer.pc.setLocalDescription(await peer.pc.createOffer());
            await send("offer", peer.pc.localDescription.toJSON(), from);
            await markReady(peer);

        } else if (message.type === "offer") {             // someone is calling us: we answer
            const peer = createPeer(from, message.name);
            await peer.pc.setRemoteDescription(message.data);
            await applyQueuedCandidates(peer);
            await peer.pc.setLocalDescription(await peer.pc.createAnswer());
            await send("answer", peer.pc.localDescription.toJSON(), from);
            await markReady(peer);

        } else if (message.type === "answer") {
            const peer = peers[from];
            if (peer && peer.pc.signalingState === "have-local-offer") {
                await peer.pc.setRemoteDescription(message.data);
                await applyQueuedCandidates(peer);
            }

        } else if (message.type === "candidate") {
            const peer = peers[from];
            if (!peer) return;
            if (peer.pc.remoteDescription) await peer.pc.addIceCandidate(message.data).catch(() => {});
            else peer.queue.push(message.data);

        } else if (message.type === "leave") {
            removePeer(from);

        } else if (message.type === "chat") {
            addChat(message.name, message.data.text, false);
        }
    }

    async function pollLoop() {
        while (running) {
            try {
                const response = await fetch(cfg.pollUrl + "?since=" + lastSignalId + "&client=" + clientId);
                if (!response.ok) throw new Error("poll failed");
                for (const message of await response.json()) {
                    lastSignalId = Math.max(lastSignalId, message.id);
                    try { await handle(message); } catch (error) { console.error(error); }
                }
            } catch (error) {
                await sleep(2000);
                continue;
            }
            await sleep(1000);
        }
    }

    // ---- chat ----
    function addChat(name, text, mine) {
        const bubble = document.createElement("div");
        bubble.className = "msg" + (mine ? " mine" : "");
        bubble.innerHTML = "<b></b><span></span>";
        bubble.children[0].textContent = mine ? "You" : name;
        bubble.children[1].textContent = text;
        chatLog.appendChild(bubble);
        chatLog.scrollTop = chatLog.scrollHeight;
    }

    document.getElementById("chat-form").addEventListener("submit", function (event) {
        event.preventDefault();
        const input = document.getElementById("chat-input");
        const text = input.value.trim();
        if (!text) return;
        input.value = "";
        addChat(cfg.myName, text, true);
        send("chat", { text: text }).catch(() => showNotice("Your message could not be sent."));
    });

    // ---- buttons ----
    function wireToggle(buttonId, getTracks) {
        const button = document.getElementById(buttonId);
        const tracks = getTracks();
        if (!tracks.length) { button.disabled = true; return; }
        button.addEventListener("click", function () {
            const turnOn = button.classList.contains("off");
            tracks.forEach(track => { track.enabled = turnOn; });
            button.classList.toggle("off", !turnOn);
        });
    }

    function leaveRoom() {
        running = false;
        const goodbye = new Blob([JSON.stringify({ type: "leave", from: clientId, to: "", data: {} })], { type: "application/json" });
        navigator.sendBeacon(cfg.signalUrl, goodbye);
        if (localStream) localStream.getTracks().forEach(track => track.stop());
        Object.values(peers).forEach(peer => peer.pc.close());
        window.location.href = cfg.exitUrl;
    }
    document.getElementById("btn-leave").addEventListener("click", leaveRoom);
    window.addEventListener("pagehide", function () {
        navigator.sendBeacon(cfg.signalUrl, new Blob([JSON.stringify({ type: "leave", from: clientId, to: "", data: {} })], { type: "application/json" }));
    });

    // ---- start ----
    async function getCameraAndMic() {
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            showNotice("Camera and microphone need a secure page (https) or localhost. You can still watch and read the chat.");
            return null;
        }
        try {
            return await navigator.mediaDevices.getUserMedia({ video: true, audio: true });
        } catch (error) {
            try {
                const audioOnly = await navigator.mediaDevices.getUserMedia({ audio: true });
                showNotice("Your camera is not available, so you are joining with audio only.");
                return audioOnly;
            } catch (error2) {
                showNotice("Camera and microphone are blocked. Allow access in your browser to speak. You can still watch and use the chat.");
                return null;
            }
        }
    }

    async function start() {
        localStream = await getCameraAndMic();

        const myTile = makeTile("me", cfg.myName, true);
        if (localStream) myTile.querySelector("video").srcObject = localStream;
        wireToggle("btn-mic", () => localStream ? localStream.getAudioTracks() : []);
        wireToggle("btn-cam", () => localStream ? localStream.getVideoTracks() : []);
        refreshRoom();

        const joined = await send("join");                 // from here on we only read messages after our own join
        lastSignalId = joined.id;
        pollLoop();

        const startedAt = Date.now();
        setInterval(function () {
            const seconds = Math.floor((Date.now() - startedAt) / 1000);
            document.getElementById("timer").textContent =
                String(Math.floor(seconds / 60)).padStart(2, "0") + ":" + String(seconds % 60).padStart(2, "0");
        }, 1000);
    }

    start().catch(function (error) {
        console.error(error);
        showNotice("Could not join the hearing room. Refresh the page to try again.");
    });
})();
