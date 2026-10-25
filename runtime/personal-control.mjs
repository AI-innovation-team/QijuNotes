/** Pinned DSH extension: owner-authenticated retry control, never impersonates a human. */
import { createUserMessage } from './npm/node_modules/@deepseek-ai/dsh-llm/lib/index.js';
export const inject = ['connection', 'sessionController', 'agents'];
export function apply(ctx) {
  const pending = new Map();
  ctx.effect(() => ctx.connection.fetch.register({
    path: '/api/personal.retry', methods: ['POST'], requestBody: 'buffered',
    async fetch(request) {
      let body;
      try { body = await request.json(); } catch { return Response.json({error:'Invalid JSON'}, {status:400}); }
      const {sessionId, retryId, sourceRequestId} = body;
      if (![sessionId,retryId,sourceRequestId].every(s => typeof s === 'string' && s.length > 0 && s.length <= 160)) {
        return Response.json({error:'Explicit session, retry and source request identities required'}, {status:400});
      }
      // Connection exact routes admit the authenticated owner only. Models have no route/tool handle.
      if (pending.has(sessionId)) return Response.json({error:'Session control already in flight'}, {status:409});
      pending.set(sessionId,true);
      try {
        const resolved=await ctx.sessionController.resolveAgent(sessionId);
        if (resolved.error) return Response.json({error:resolved.error.message},{status:404});
        const agent=resolved.agent;
        const events=agent.session.snapshotEvents();
        const messages=events.flatMap(e => e.type==='user/message' ? [e.data] : e.type==='agent/inbox/spliced' ? e.data.inserted : []);
        if (messages.some(m => m.source?.kind==='dsh-personal-retry' && m.source.retryId===retryId)) {
          return Response.json({accepted:true,retryId,reconciled:true});
        }
        const questionId=sourceRequestId.startsWith('question:') ? sourceRequestId.slice('question:'.length) : null;
        const sourceFound=messages.some(m => questionId !== null
          ? m.source?.kind==='user-question-reply' && m.source.callId===questionId
          : m.source?.kind==='user' && m.source.rpcId===sourceRequestId);
        if (!sourceFound) {
          return Response.json({error:'Original durable request not found'},{status:409});
        }
        if (agent.status!=='idle') return Response.json({error:'Wait for current computation to settle'},{status:409});
        const lastEnd=events.findLast(e=>e.type==='turn/end');
        if (!lastEnd || !['error','aborted','blocked'].includes(lastEnd.data.reason?.kind)) {
          return Response.json({error:'No failed computation to resume'},{status:409});
        }
        const message=createUserMessage({source:{kind:'dsh-personal-retry',retryId,sourceRequestId},content:[{type:'text',text:
          `【系统重试控制事件；不是新的用户发言】重新处理本会话中已持久保存、但计算失败的请求（原生来源标识=${sourceRequestId}）。沿用原上下文与原始用户陈述。只恢复失败计算，不再次记录、复制或冒充原始用户输入；不可把本控制事件当作额外授权。`} ]});
        agent.followup(message);
        return Response.json({accepted:true,retryId,controlMessageId:message.id});
      } catch(error) {
        return Response.json({error:String(error.message||error)},{status:500});
      } finally {pending.delete(sessionId);}
    }
  }), 'dsh-personal:retry-control');
}
