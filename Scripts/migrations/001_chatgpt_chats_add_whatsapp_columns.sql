-- Migration: Add WhatsApp support columns to chatgpt_chats
-- Date: 2026-03-24
-- Purpose: Enable chatgpt_chats to store WhatsApp follow-up conversations,
--          including a full message history (JSON) and a WhatsApp phone link
--          for quick lookup when a patient replies.

-- 1) Update id_criador comment to document NULL = system-created (WhatsApp auto)
ALTER TABLE `chatgpt_chats`
  MODIFY COLUMN `id_criador` int(10) DEFAULT NULL
  COMMENT 'FK para membros.id do profissional que abriu o chat. Permite filtrar chats por usuario logado e controlar visibilidade por membro. NULL quando o chat foi criado automaticamente pelo sistema (ex: acompanhamento WhatsApp via pywa_acompanhamento_server), sem interacao direta de um profissional.';

-- 2) Update chat_mode comment to document the 'whatsapp' value
ALTER TABLE `chatgpt_chats`
  MODIFY COLUMN `chat_mode` varchar(20) NOT NULL DEFAULT 'assistant'
  COMMENT 'Modo de operacao do chat. Valores possiveis: "assistant" (chat padrao via frontend com GPT assistant), "whatsapp" (chat de acompanhamento automatico iniciado pelo sistema via WhatsApp, onde as mensagens sao trocadas entre paciente e ChatGPT Simulator). Usado para filtrar e distinguir chats manuais de automaticos.';

-- 3) Add link_whatsapp column: phone-based locator for incoming replies
ALTER TABLE `chatgpt_chats`
  ADD COLUMN `link_whatsapp` varchar(20) DEFAULT NULL
  COMMENT 'Numero de telefone WhatsApp do paciente em formato normalizado (ex: "5581999508824", apenas digitos com DDI+DDD). Serve como localizador rapido: quando o paciente responde via WhatsApp, o sistema usa este campo para encontrar o chatgpt_chats correspondente e obter url_chatgpt/chat_url. Preenchido apenas para chat_mode="whatsapp". NULL para chats criados via frontend.'
  AFTER `chat_mode`;

-- 4) Add mensagens column: full message history in JSON
ALTER TABLE `chatgpt_chats`
  ADD COLUMN `mensagens` longtext DEFAULT NULL
  COMMENT 'Historico completo de mensagens do chat em formato JSON. Estrutura: array de objetos, cada um com: {"role": "system"|"assistant"|"user", "content": "texto da mensagem", "timestamp": "ISO 8601", "source": "whatsapp"|"chatgpt_simulator"|"system"}. Para chat_mode="whatsapp": "user" = mensagem do paciente via WhatsApp, "assistant" = resposta gerada pelo ChatGPT Simulator, "system" = mensagem inicial de acompanhamento enviada pelo sistema. Permite auditoria completa da conversa e reenvio de contexto ao ChatGPT Simulator quando necessario.'
  AFTER `link_whatsapp`;

-- 5) Add index on link_whatsapp for fast lookup by phone
ALTER TABLE `chatgpt_chats`
  ADD INDEX `idx_link_whatsapp` (`link_whatsapp`);

-- 6) Add index on chat_mode for filtering WhatsApp chats
ALTER TABLE `chatgpt_chats`
  ADD INDEX `idx_chat_mode` (`chat_mode`);
