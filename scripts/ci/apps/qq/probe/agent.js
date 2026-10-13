const generations = new Map();
const depths = new Map();
const maximum = 262144;
function sizeAt(pointer) {
  return Process.pointerSize === 8 ? Number(pointer.readU64()) : pointer.readU32();
}
function vectors(pointer, count) {
  if (count < 0 || count > 32) throw new Error('Invalid iovec count');
  const parts = [];
  let total = 0;
  for (let i = 0; i < count; i++) {
    const entry = pointer.add(i * Process.pointerSize * 2);
    const length = sizeAt(entry.add(Process.pointerSize));
    total += length;
    if (total > maximum) throw new Error('iovec exceeds capture limit');
    if (length) parts.push(new Uint8Array(entry.readPointer().readByteArray(length)));
  }
  const result = new Uint8Array(total);
  let offset = 0;
  for (const part of parts) { result.set(part, offset); offset += part.length; }
  return result.buffer;
}
for (const name of ['send', 'sendto', 'write', 'writev', 'sendmsg']) {
  const target = Module.findGlobalExportByName(name);
  if (!target) continue;
  Interceptor.attach(target, {
    onEnter(args) {
      this.thread = Process.getCurrentThreadId();
      const depth = depths.get(this.thread) || 0;
      depths.set(this.thread, depth + 1);
      this.bytes = null;
      if (depth) return;
      try {
        this.fd = args[0].toInt32();
        const type = Socket.type(this.fd);
        if (type !== 'tcp' && type !== 'tcp6') return;
        this.generation = generations.get(this.fd) || 0;
        if (name === 'writev') this.bytes = vectors(args[1], args[2].toInt32());
        else if (name === 'sendmsg') {
          const offset = Process.pointerSize === 8 ? 16 : 8;
          this.bytes = vectors(args[1].add(offset).readPointer(),
                               sizeAt(args[1].add(offset + Process.pointerSize)));
        } else {
          const length = parseInt(args[2].toString(), 16);
          if (length > maximum) throw new Error('Send exceeds capture limit');
          if (length) this.bytes = args[1].readByteArray(length);
        }
      } catch (error) { send({kind: 'capture-error'}); }
    },
    onLeave(result) {
      const depth = (depths.get(this.thread) || 1) - 1;
      if (depth) depths.set(this.thread, depth); else depths.delete(this.thread);
      const written = result.toInt32();
      if (this.bytes && written > 0) {
        if (written > this.bytes.byteLength) { send({kind: 'capture-error'}); return; }
        send({kind: 'bytes', fd: this.fd, generation: this.generation}, this.bytes.slice(0, written));
      }
    }
  });
}
const close = Module.findGlobalExportByName('close');
if (close) Interceptor.attach(close, {
  onEnter(args) { this.fd = args[0].toInt32(); },
  onLeave(result) {
    if (result.toInt32() === 0) generations.set(this.fd, (generations.get(this.fd) || 0) + 1);
  }
});
send({kind: 'ready', pid: Process.id});
