// OpenCode 1.18.30 plugin bridge. It writes only fixed lifecycle metadata.
import { appendFileSync, lstatSync, statSync } from 'node:fs'
import { randomUUID } from 'node:crypto'
import { dirname, isAbsolute } from 'node:path'

export default async function opencodeAttention() {
  const queue = process.env.WB_OPENCODE_ATTENTION_EVENTS
  const sourceInstance = randomUUID()
  const sessions = new Map()

  function append(record) {
    if (!queue) return
    if (!isAbsolute(queue)) throw new Error('WB_OPENCODE_ATTENTION_EVENTS must be absolute')
    const parent = statSync(dirname(queue))
    if (!parent.isDirectory() || parent.mode & 0o077 || parent.uid !== process.getuid()) {
      throw new Error('OpenCode attention queue directory must be private and owned by this user')
    }
    try {
      const file = lstatSync(queue)
      if (!file.isFile() || file.isSymbolicLink() || file.uid !== process.getuid() || file.mode & 0o077) {
        throw new Error('OpenCode attention queue must be a private, owned regular file')
      }
    } catch (error) {
      if (error.code !== 'ENOENT') throw error
    }
    appendFileSync(queue, JSON.stringify(record)+'\n', {encoding: 'utf8', flag: 'a', mode: 0o600})
  }

  function generation(sessionID, messageID, created) {
    if (!sessionID || !messageID || typeof created !== 'number') return
    const current = sessions.get(sessionID)
    if (current?.id === messageID) return
    const state = {id: messageID, sequence: 0, lastTime: created, messages: new Map(), calls: new Map(), busy: false, emitted: new Set()}
    append({kind: 'generation', provider: 'opencode', session_id: sessionID,
      generation_id: messageID, source: 'opencode-plugin', source_instance: sourceInstance,
      started_at: new Date(created).toISOString(), provenance: 'opencode.chat.message'})
    sessions.set(sessionID, state)
  }

  function observe(sessionID, generationID, reason, provenance, key) {
    const state = sessions.get(sessionID)
    if (!state || state.id !== generationID || state.emitted.has(key)) return
    const sequence = state.sequence+1
    const observed = Math.max(Date.now(), state.lastTime+1)
    append({kind: 'observation', provider: 'opencode', session_id: sessionID,
      generation_id: state.id, source: 'opencode-plugin', source_instance: sourceInstance,
      sequence, observed_at: new Date(observed).toISOString(), reason, provenance})
    state.emitted.add(key)
    state.sequence = sequence
    state.lastTime = observed
  }

  return {
    'chat.message': async (input, output) => {
      generation(input.sessionID, input.messageID || output.message?.id, output.message?.time?.created)
    },
    event: async ({event}) => {
      if (event.type === 'message.updated' && event.properties?.info?.role === 'assistant') {
        const info = event.properties.info
        const state = sessions.get(info.sessionID)
        if (!state || state.id !== info.parentID) return
        state.messages.set(info.id, info.parentID)
        if (info.error) {
          observe(info.sessionID, info.parentID, 'provider_error', 'opencode.message.error', 'error:'+info.id)
        }
      }
      if (event.type === 'message.part.updated' && event.properties?.part?.type === 'tool') {
        const part = event.properties.part
        const state = sessions.get(part.sessionID)
        const generationID = state?.messages.get(part.messageID)
        if (state && generationID === state.id) state.calls.set(part.callID, generationID)
      }
      if (event.type === 'permission.updated') {
        const permission = event.properties
        const state = sessions.get(permission.sessionID)
        const generationID = state?.messages.get(permission.messageID)
        observe(permission.sessionID, generationID, 'permission_wait', 'opencode.permission.updated', 'permission:'+permission.id)
      }
      if (event.type === 'session.status') {
        const {sessionID, status} = event.properties
        const state = sessions.get(sessionID)
        if (!state) return
        if (status?.type === 'busy') state.busy = true
        if (status?.type === 'idle' && state.busy) {
          observe(sessionID, state.id, 'idle', 'opencode.session.idle', 'idle')
          state.busy = false
        }
      }
    },
    'tool.execute.before': async (input) => {
      if (input.tool !== 'question') return
      const state = sessions.get(input.sessionID)
      const generationID = state?.calls.get(input.callID)
      observe(input.sessionID, generationID, 'user_question', 'opencode.tool.question', 'question:'+input.callID)
    },
  }
}
