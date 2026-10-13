import { describe, expect, it } from 'vitest';

import { createUnpackDiagnosticsGuard } from './unpackDiagnosticsGuard';

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

describe('unpack diagnostics request ownership', () => {
  it('rejects an older historical result when another movie is opened', async () => {
    const guard = createUnpackDiagnosticsGuard();
    const old = deferred<string>();
    const itemA = guard.open('item-a');
    const display: string[] = [];
    const oldRequest = old.promise.then((value) => {
      if (guard.isCurrent(itemA)) display.push(value);
    });
    const itemB = guard.open('item-b');
    expect(guard.isCurrent(itemA)).toBe(false);
    expect(guard.isCurrent(itemB)).toBe(true);
    old.resolve('wrong movie A');
    await oldRequest;
    expect(display).toEqual([]);
  });

  it('rejects a stale live result even when the same item is reopened', async () => {
    const guard = createUnpackDiagnosticsGuard();
    const old = deferred<string>();
    const first = guard.open('item-a');
    const display: string[] = [];
    const liveRequest = old.promise.then((value) => {
      if (guard.isCurrent(first)) display.push(value);
    });
    guard.close();
    const reopened = guard.open('item-a');
    old.resolve('stale client matched');
    await liveRequest;
    expect(guard.isCurrent(first)).toBe(false);
    expect(guard.isCurrent(reopened)).toBe(true);
    expect(display).toEqual([]);
  });

  it('invalidates both historical and live responses immediately when closed', () => {
    const guard = createUnpackDiagnosticsGuard();
    const ticket = guard.open('item-a');
    expect(guard.capture('item-a')).toEqual(ticket);
    expect(guard.capture('item-b')).toBeNull();
    guard.close();
    expect(guard.isCurrent(ticket)).toBe(false);
    expect(guard.capture('item-a')).toBeNull();
  });
});
