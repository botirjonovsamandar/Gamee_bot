import { useEffect, useRef, useState } from "react";
import type { WsEvent } from "../types";

export type SocketState = "connecting" | "online" | "reconnecting" | "offline";

function wsUrl(): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}/ws/events`;
}

export function useWebSocketEvents(onEvent: (event: WsEvent) => void): SocketState {
  const [state, setState] = useState<SocketState>("connecting");
  const handler = useRef(onEvent);

  useEffect(() => {
    handler.current = onEvent;
  }, [onEvent]);

  useEffect(() => {
    let stopped = false;
    let retry: number | undefined;
    let socket: WebSocket | undefined;

    const connect = () => {
      if (stopped) return;
      setState((cur) => (cur === "connecting" ? "connecting" : "reconnecting"));
      socket = new WebSocket(wsUrl());
      socket.onopen = () => setState("online");
      socket.onmessage = (msg) => {
        try {
          handler.current(JSON.parse(msg.data) as WsEvent);
        } catch {
          // Ignore malformed events.
        }
      };
      socket.onclose = () => {
        if (stopped) return;
        setState("reconnecting");
        retry = window.setTimeout(connect, 1200);
      };
      socket.onerror = () => {
        socket?.close();
      };
    };

    connect();
    return () => {
      stopped = true;
      setState("offline");
      if (retry !== undefined) window.clearTimeout(retry);
      socket?.close();
    };
  }, []);

  return state;
}

