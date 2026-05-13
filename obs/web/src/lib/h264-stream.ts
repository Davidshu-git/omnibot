export type StreamPlayerStatus =
  | "connecting"
  | "waiting-keyframe"
  | "playing"
  | "restarting"
  | "error"
  | "closed";

export interface StreamPlayerOptions {
  url: string;
  canvas: HTMLCanvasElement;
  onStatus?: (status: StreamPlayerStatus, detail?: string) => void;
  onResolution?: (width: number, height: number) => void;
}

type Bytes = Uint8Array<ArrayBufferLike>;

type VideoFrameLike = {
  displayWidth: number;
  displayHeight: number;
  close: () => void;
};

type VideoDecoderLike = {
  configure: (config: {
    codec: string;
    description?: Uint8Array;
    optimizeForLatency?: boolean;
  }) => void;
  decode: (chunk: unknown) => void;
  close: () => void;
};

type WebCodecsWindow = Window & {
  VideoDecoder?: new (init: {
    output: (frame: VideoFrameLike) => void;
    error: (error: unknown) => void;
  }) => VideoDecoderLike;
  EncodedVideoChunk?: new (init: {
    type: "key" | "delta";
    timestamp: number;
    data: Bytes;
  }) => unknown;
};

function webCodecsWindow(): WebCodecsWindow | null {
  if (typeof window === "undefined") return null;
  return window as WebCodecsWindow;
}

function concatBytes(a: Bytes, b: Bytes): Bytes {
  const out = new Uint8Array(a.length + b.length);
  out.set(a, 0);
  out.set(b, a.length);
  return out;
}

function startCodeLength(bytes: Bytes, index: number): number {
  if (index + 3 <= bytes.length
    && bytes[index] === 0
    && bytes[index + 1] === 0
    && bytes[index + 2] === 1) {
    return 3;
  }
  if (index + 4 <= bytes.length
    && bytes[index] === 0
    && bytes[index + 1] === 0
    && bytes[index + 2] === 0
    && bytes[index + 3] === 1) {
    return 4;
  }
  return 0;
}

function avccChunk(nals: Bytes[]): Bytes {
  const totalLen = nals.reduce((sum, nal) => sum + 4 + nal.length, 0);
  const out = new Uint8Array(totalLen);
  let offset = 0;
  for (const nal of nals) {
    const len = nal.length;
    out[offset] = (len >>> 24) & 0xff;
    out[offset + 1] = (len >>> 16) & 0xff;
    out[offset + 2] = (len >>> 8) & 0xff;
    out[offset + 3] = len & 0xff;
    out.set(nal, offset + 4);
    offset += 4 + len;
  }
  return out;
}

function avcDescription(sps: Bytes, pps: Bytes): Bytes {
  const out = new Uint8Array(11 + sps.length + pps.length);
  let offset = 0;
  out[offset++] = 0x01;
  out[offset++] = sps[1];
  out[offset++] = sps[2];
  out[offset++] = sps[3];
  out[offset++] = 0xff;
  out[offset++] = 0xe1;
  out[offset++] = (sps.length >> 8) & 0xff;
  out[offset++] = sps.length & 0xff;
  out.set(sps, offset);
  offset += sps.length;
  out[offset++] = 0x01;
  out[offset++] = (pps.length >> 8) & 0xff;
  out[offset++] = pps.length & 0xff;
  out.set(pps, offset);
  return out;
}

function rbspBytes(nal: Bytes): Bytes {
  const out: number[] = [];
  for (let i = 1; i < nal.length; i++) {
    if (i + 2 < nal.length && nal[i] === 0 && nal[i + 1] === 0 && nal[i + 2] === 3) {
      out.push(0, 0);
      i += 2;
      continue;
    }
    out.push(nal[i]);
  }
  return new Uint8Array(out);
}

function readUE(bytes: Bytes): number | null {
  let bit = 0;
  const readBit = (): number | null => {
    const byteIndex = bit >> 3;
    if (byteIndex >= bytes.length) return null;
    const value = (bytes[byteIndex] >> (7 - (bit & 7))) & 1;
    bit += 1;
    return value;
  };

  let zeros = 0;
  while (true) {
    const b = readBit();
    if (b === null) return null;
    if (b === 1) break;
    zeros += 1;
    if (zeros > 31) return null;
  }

  let value = 1;
  for (let i = 0; i < zeros; i++) {
    const b = readBit();
    if (b === null) return null;
    value = (value << 1) | b;
  }
  return value - 1;
}

function firstMbInSlice(nal: Bytes): number | null {
  const type = nal[0] & 0x1f;
  if (type !== 1 && type !== 5) return null;
  return readUE(rbspBytes(nal));
}

export function isWebCodecsSupported(): boolean {
  const wc = webCodecsWindow();
  return Boolean(wc?.VideoDecoder && wc?.EncodedVideoChunk);
}

export class StreamPlayer {
  private ws?: WebSocket;
  private decoder?: VideoDecoderLike;
  private ctx: CanvasRenderingContext2D | null;
  private buffer: Bytes = new Uint8Array(0);
  private sps?: Bytes;
  private pps?: Bytes;
  private configured = false;
  private gotFirstKeyframe = false;
  private closed = false;
  private accessUnit: Bytes[] = [];
  private accessUnitHasVcl = false;
  private accessUnitHasIdr = false;

  constructor(private opts: StreamPlayerOptions) {
    this.ctx = opts.canvas.getContext("2d");
    this.connect();
  }

  close(): void {
    this.closed = true;
    try { this.ws?.close(); } catch {}
    try { this.decoder?.close(); } catch {}
    this.opts.onStatus?.("closed");
  }

  private connect(): void {
    if (!isWebCodecsSupported()) {
      this.opts.onStatus?.("error", "browser does not support WebCodecs");
      return;
    }
    this.opts.onStatus?.("connecting");
    const ws = new WebSocket(this.opts.url);
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onmessage = (event) => {
      if (typeof event.data === "string") {
        this.handleControlMessage(event.data);
        return;
      }
      this.ingest(new Uint8Array(event.data));
    };
    ws.onerror = () => this.opts.onStatus?.("error", "websocket error");
    ws.onclose = () => {
      if (!this.closed) this.opts.onStatus?.("closed");
    };
  }

  private handleControlMessage(data: string): void {
    try {
      const msg = JSON.parse(data) as { event?: string };
      if (msg.event === "restart") this.handleRestart();
    } catch {
      this.opts.onStatus?.("error", "invalid stream control message");
    }
  }

  private handleRestart(): void {
    try { this.decoder?.close(); } catch {}
    this.decoder = undefined;
    this.buffer = new Uint8Array(0);
    this.sps = undefined;
    this.pps = undefined;
    this.configured = false;
    this.gotFirstKeyframe = false;
    this.accessUnit = [];
    this.accessUnitHasVcl = false;
    this.accessUnitHasIdr = false;
    this.opts.onStatus?.("restarting");
  }

  private splitNALs(chunk: Bytes): Bytes[] {
    const merged = concatBytes(this.buffer, chunk);
    const starts: { prefix: number; nal: number }[] = [];
    for (let i = 0; i < merged.length - 2; i++) {
      const len = startCodeLength(merged, i);
      if (len > 0) {
        starts.push({ prefix: i, nal: i + len });
        i += len - 1;
      }
    }

    if (starts.length === 0) {
      this.buffer = merged;
      return [];
    }

    const nals: Bytes[] = [];
    for (let i = 0; i + 1 < starts.length; i++) {
      const nal = merged.subarray(starts[i].nal, starts[i + 1].prefix);
      if (nal.length > 0) nals.push(nal);
    }
    this.buffer = merged.subarray(starts[starts.length - 1].prefix);
    return nals;
  }

  private ingest(chunk: Bytes): void {
    const nals = this.splitNALs(chunk);
    if (nals.length === 0) return;

    for (const nal of nals) {
      const type = nal[0] & 0x1f;
      if (type === 7) this.sps = nal;
      else if (type === 8) this.pps = nal;
      this.enqueueNal(nal);
    }
  }

  private enqueueNal(nal: Bytes): void {
    const type = nal[0] & 0x1f;

    // Access Unit Delimiter is the clean frame boundary emitted by Android screenrecord.
    if (type === 9) {
      this.flushAccessUnit();
      return;
    }

    // Fallback for streams without AUD: a new VCL NAL with first_mb_in_slice=0
    // starts a new picture after the current access unit already has VCL data.
    if ((type === 1 || type === 5) && this.accessUnitHasVcl && firstMbInSlice(nal) === 0) {
      this.flushAccessUnit();
    }

    if (type === 1 || type === 5) {
      this.accessUnit.push(nal);
      this.accessUnitHasVcl = true;
      if (type === 5) this.accessUnitHasIdr = true;
      return;
    }

    // Parameter sets are carried in VideoDecoderConfig.description. SEI is optional
    // for display and is skipped to keep samples minimal.
  }

  private flushAccessUnit(): void {
    if (!this.accessUnitHasVcl || this.accessUnit.length === 0) {
      this.accessUnit = [];
      this.accessUnitHasVcl = false;
      this.accessUnitHasIdr = false;
      return;
    }

    const nals = this.accessUnit;
    const hasIdr = this.accessUnitHasIdr;
    this.accessUnit = [];
    this.accessUnitHasVcl = false;
    this.accessUnitHasIdr = false;

    if (!this.configured && this.sps && this.pps) this.configureDecoder();
    if (!this.configured) return;

    if (!this.gotFirstKeyframe) {
      if (!hasIdr) {
        this.opts.onStatus?.("waiting-keyframe");
        return;
      }
      this.gotFirstKeyframe = true;
      this.opts.onStatus?.("playing");
    }

    const wc = webCodecsWindow();
    if (!wc?.EncodedVideoChunk) return;
    try {
      this.decoder?.decode(new wc.EncodedVideoChunk({
        type: hasIdr ? "key" : "delta",
        timestamp: Math.round(performance.now() * 1000),
        data: avccChunk(nals),
      }));
    } catch (error) {
      console.error("H.264 stream decode failed", error);
      this.opts.onStatus?.("error", String(error));
    }
  }

  private configureDecoder(): void {
    const sps = this.sps;
    const pps = this.pps;
    const wc = webCodecsWindow();
    if (!sps || !pps || !wc?.VideoDecoder) return;
    if (sps.length < 4) {
      this.opts.onStatus?.("error", "invalid SPS");
      return;
    }

    const codec = `avc1.${[sps[1], sps[2], sps[3]]
      .map((b) => b.toString(16).padStart(2, "0"))
      .join("")
      .toUpperCase()}`;

    this.decoder = new wc.VideoDecoder({
      output: (frame) => {
        if (this.closed) {
          frame.close();
          return;
        }
        const canvas = this.opts.canvas;
        if (canvas.width !== frame.displayWidth || canvas.height !== frame.displayHeight) {
          canvas.width = frame.displayWidth;
          canvas.height = frame.displayHeight;
          this.opts.onResolution?.(frame.displayWidth, frame.displayHeight);
        }
        this.ctx?.drawImage(frame as unknown as CanvasImageSource, 0, 0);
        frame.close();
      },
      error: (error) => {
        console.error("H.264 VideoDecoder error", error);
        this.opts.onStatus?.("error", String(error));
      },
    });
    this.decoder.configure({
      codec,
      description: avcDescription(sps, pps),
      optimizeForLatency: true,
    });
    this.configured = true;
    this.opts.onStatus?.("waiting-keyframe");
  }
}
