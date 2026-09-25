import { readFile } from 'node:fs/promises'
import { randomUUID } from 'node:crypto'

const html = await readFile(new URL('./index.html', import.meta.url), 'utf8')

export const inject = ['webServer', 'connection', 'sessionController', 'workspaceController']

export function apply(ctx) {
  const page = ctx.webServer.register({
    kind: 'exact',
    path: '/harness-pi',
    handler(req, res) {
      if (!ctx.connection.authorizeIndex({ headers: req.headers, method: req.method, url: req.url }, res)) return
      if (req.method !== 'GET' && req.method !== 'HEAD') {
        res.writeHead(405, { allow: 'GET, HEAD' })
        res.end()
        return
      }
      res.writeHead(200, {
        'content-type': 'text/html; charset=utf-8',
        'cache-control': 'no-store',
        'content-security-policy': "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; img-src data:; base-uri 'none'; frame-ancestors 'none'",
        'x-content-type-options': 'nosniff',
      })
      res.end(req.method === 'HEAD' ? undefined : html)
    },
  })

  const api = ctx.connection.fetch.register({
    path: '/harness-pi',
    methods: ['POST'],
    requestBody: 'buffered',
    async fetch(req) {
      try {
        const body = await req.json()
        let value
        if (body.action === 'status') {
          value = { ready: true }
        } else if (body.action === 'workspace') {
          if (typeof body.path !== 'string' || !body.path.trim()) throw new Error('Informe o caminho local do projeto.')
          const result = await ctx.workspaceController.create({ path: body.path.trim() })
          value = result.workspace
        } else if (body.action === 'sessions') {
          value = await ctx.sessionController.list({}, req.signal)
        } else if (body.action === 'create') {
          if (typeof body.workspaceId !== 'string' || !body.workspaceId) throw new Error('Workspace inválido.')
          value = await ctx.sessionController.create({ workspaceId: body.workspaceId })
        } else if (body.action === 'prompt') {
          if (typeof body.sessionId !== 'string' || !body.sessionId || typeof body.text !== 'string' || !body.text.trim()) {
            throw new Error('A solicitação está vazia ou a sessão é inválida.')
          }
          value = await ctx.sessionController.prompt({
            requestId: randomUUID(),
            sessionId: body.sessionId,
            mode: 'queue',
            content: [{ type: 'text', text: body.text }],
            clientTimeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
          }, req.signal)
        } else if (body.action === 'read') {
          if (typeof body.sessionId !== 'string' || !body.sessionId) throw new Error('Sessão inválida.')
          const projection = await ctx.sessionController.projections({ sessionId: body.sessionId }, req.signal)
          const page = await ctx.sessionController.page({
            address: { kind: 'session', sessionId: body.sessionId },
            throughSeq: projection?.asOfSeq ?? -1,
            maxMessages: 50,
          }, req.signal)
          const sessions = await ctx.sessionController.list({}, req.signal)
          value = {
            page,
            running: sessions.items.find(item => item.sessionId === body.sessionId)?.running === true,
          }
        } else {
          return Response.json({ ok: false, error: 'Ação desconhecida.' }, { status: 404 })
        }
        return Response.json({ ok: true, value }, { headers: { 'cache-control': 'no-store' } })
      } catch (error) {
        return Response.json({
          ok: false,
          error: error instanceof Error ? error.message : String(error),
        }, { status: 400, headers: { 'cache-control': 'no-store' } })
      }
    },
  })

  ctx.effect(() => () => {
    page()
    return api()
  }, 'harness-pi-client.routes')
}
