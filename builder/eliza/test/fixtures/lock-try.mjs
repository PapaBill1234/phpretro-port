// SPDX-License-Identifier: GPL-3.0-only
// Test probe: attempts the real OS runtime lock once and releases immediately.
// Exit 0 proves a clean (re)acquisition; a throw (blocked) exits non-zero.
import { singleton } from '../../src/process.mjs';
const release = await singleton(process.argv[2]);
await release();
