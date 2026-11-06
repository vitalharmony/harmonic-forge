/**
 * Sends a Lane 2 request on the high-tier model while this session holds a route-floor
 * marker (harmonic-forge#1250).
 *
 * The issue is known at assignment and at a write, never at request time, so the hooks
 * that know it write the marker (`tools/hooks/route_floor_marker.py`) and this only looks
 * for it: no subprocess, no network. It never lowers a model, and any failure sends the
 * request on the model it came with, because `model_tier_gate.py` still reads the real
 * model and denies independently.
 */
import type { Register, TurnStepInput } from 'claude-code'

/** The same families as `CLAUDE_HIGH_FAMILIES` in `model_tier_gate.py`. */
const HIGH_FAMILY = /opus|fable/i
const FLOOR_MODEL = 'opus'

export const register: Register = on => {
  on('turn.step', async function* ($, e, next) {
    let step: TurnStepInput = e
    if (!HIGH_FAMILY.test(e.model)) {
      try {
        const home = await $.env.get('HOME')
        const id = await $.session.id()
        if (await $.fs.exists(`${home}/.cache/harmonic-forge/route_floor/${id}`)) {
          step = { ...e, model: FLOOR_MODEL }
        }
      } catch (error) {
        $.ui.log(`kept ${e.model}: ${(error as Error).message}`, { to: 'debug' })
      }
    }
    return yield* next(step)
  })
}
