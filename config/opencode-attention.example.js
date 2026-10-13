// OpenCode 1.18.30 plugin bridge. It writes only fixed lifecycle metadata.
import { appendFileSync, lstatSync, statSync } from 'node:fs'
import { createHash, randomUUID } from 'node:crypto'
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
    const state = {id: messageID, sequence: 0, lastTime: created, messages: new Map(), calls: new Map(),
      permissions: new Map(), terminalCalls: new Set(), emitted: new Set()}
    append({kind: 'generation', provider: 'opencode', session_id: sessionID,
      generation_id: messageID, source: 'opencode-plugin', source_instance: sourceInstance,
      started_at: new Date(created).toISOString(), provenance: 'opencode.chat.message'})
    sessions.set(sessionID, state)
  }

  function observe(sessionID, generationID, reason, provenance, identity, stateName = 'open') {
    const state = sessions.get(sessionID)
    if (!state || state.id !== generationID) return
    const key = stateName+':'+reason+':'+identity
    if (stateName === 'resolved' && !state.emitted.has('open:'+reason+':'+identity)) return
    if (state.emitted.has(key)) return
    const sequence = state.sequence+1
    const observed = Math.max(Date.now(), state.lastTime+1)
    append({kind: 'observation', provider: 'opencode', session_id: sessionID,
      generation_id: state.id, source: 'opencode-plugin', source_instance: sourceInstance,
      incident_id: createHash('sha256').update(reason+':'+identity).digest('hex'),
      sequence, observed_at: new Date(observed).toISOString(), reason, state: stateName, provenance})
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
          observe(info.sessionID, info.parentID, 'provider_error', 'opencode.message.error', info.id)
        }
      }
      if (event.type === 'message.part.updated' && event.properties?.part?.type === 'tool') {
        const part = event.properties.part
        const state = sessions.get(part.sessionID)
        const generationID = state?.messages.get(part.messageID)
        if (state && generationID === state.id) {
          state.calls.set(part.callID, generationID)
          if (part.tool === 'question' && ['completed', 'error'].includes(part.state?.status)) {
            observe(part.sessionID, generationID, 'user_question', 'opencode.question.completed', part.callID, 'resolved')
            state.terminalCalls.add(part.callID)
          }
        }
      }
      if (event.type === 'permission.asked') {
        const permission = event.properties
        const state = sessions.get(permission.sessionID)
        const generationID = state?.messages.get(permission.tool?.messageID)
        if (state && generationID === state.id) state.permissions.set(permission.id, generationID)
        observe(permission.sessionID, generationID, 'permission_wait', 'opencode.permission.asked', permission.id)
      }
      if (event.type === 'permission.replied') {
        const reply = event.properties
        const state = sessions.get(reply.sessionID)
        const generationID = state?.permissions.get(reply.requestID)
        observe(reply.sessionID, generationID, 'permission_wait', 'opencode.permission.replied', reply.requestID, 'resolved')
      }
    },
    'tool.execute.before': async (input) => {
      if (input.tool !== 'question') return
      const state = sessions.get(input.sessionID)
      if (state?.terminalCalls.has(input.callID)) return
      const generationID = state?.calls.get(input.callID)
      observe(input.sessionID, generationID, 'user_question', 'opencode.tool.question', input.callID)
    },
  }
}
