import { expect, test } from 'claude-code/testing'

const STEP = { turnId: 't', index: 0, model: 'sonnet', messageCount: 1 }
const HOME = '/home/someone'
const ID = 'session-1'
/** `tools/routing/test_floor_marker_contract.py` checks this against the Python writer. */
const MARKER = '/home/someone/.cache/harmonic-forge/route_floor/session-1'

/** Stubs the engine beneath the mod. `exists` answers `$.fs.exists`. */
function harness(on: any, exists: () => unknown) {
  const seen = { sent: '', asked: [] as string[], logs: [] as { text: string; to?: string }[] }
  on('env.get', () => ({ value: HOME }))
  on('session.id', () => ({ value: ID }))
  on('fs.exists', (_: unknown, e: { path: string }) => {
    seen.asked.push(e.path)
    return exists()
  })
  on('ui.log', (_: unknown, e: { text: string; to?: string }) => {
    seen.logs.push({ text: e.text, to: e.to })
    return { value: undefined }
  })
  on('turn.step', async function* (_: unknown, e: { turnId: string; index: number; model: string }) {
    seen.sent = e.model
    return { turnId: e.turnId, index: e.index, answer: '', toolUses: [], stopReason: 'end_turn', usage: null }
  })
  return seen
}

async function step($: any, input: object = {}) {
  for await (const _ of $.turn.step({ ...STEP, ...input })) { /* no chunks */ }
}

test("AC5: this session's marker raises a sonnet request to opus", async ($, on) => {
  const seen = harness(on, () => ({ value: true }))
  await step($)
  expect(seen.sent).toBe('opus')
})

test('AC5: a subagent step is raised on the same marker', async ($, on) => {
  const seen = harness(on, () => ({ value: true }))
  await step($, { agentId: 'sub-1' })
  expect(seen.sent).toBe('opus')
})

test('AC5: no marker leaves the model alone and logs nothing', async ($, on) => {
  const seen = harness(on, () => ({ value: false }))
  await step($)
  expect(seen.sent).toBe('sonnet')
  expect(seen.logs).toEqual([])
})

test('AC5: a failed read keeps the model and logs one debug line', async ($, on) => {
  const seen = harness(on, () => { throw new Error('no such thing') })
  await step($)
  expect(seen.sent).toBe('sonnet')
  expect(seen.logs.length).toBe(1)
  expect(seen.logs[0].to).toBe('debug')
  expect(seen.logs[0].text).toMatch(/^kept sonnet: /)
})

for (const model of ['opus', 'claude-opus-5-5', 'claude-fable-5-1']) {
  test(`AC5: ${model} is untouched and asks nothing`, async ($, on) => {
    const seen = harness(on, () => ({ value: true }))
    await step($, { model })
    expect(seen.sent).toBe(model)
    expect(seen.asked).toEqual([])
  })
}

test('the path it looks for is the one the Python writer writes', async ($, on) => {
  const seen = harness(on, () => ({ value: false }))
  await step($)
  expect(seen.asked).toEqual([MARKER])
})
