-- Migration: Add notificacao_pendente column to chatgpt_chats
-- Date: 2026-04-08
-- Purpose: Enable notification system for when the ChatGPT Simulator/LLM
--          indicates it needs to consult a professional (Dr/Dra) or secretary
--          about a patient inquiry. This column flags the chat so the system
--          can alert the appropriate user (creator or secretary) to review
--          and respond directly to the patient.

-- 1) Add notificacao_pendente column
ALTER TABLE `chatgpt_chats`
  ADD COLUMN IF NOT EXISTS `notificacao_pendente` varchar(20) NOT NULL DEFAULT 'false'
  COMMENT 'Flag de notificacao pendente para profissional ou secretaria. Valores possiveis: "false" (sem pendencia), "id_criador" (notificacao direcionada ao profissional criador do atendimento — o sistema exibe alerta ao usuario cujo membros.id corresponda a chatgpt_chats.id_criador deste registro), "id_secretaria" (notificacao direcionada a secretarias — o sistema identifica secretarias por: membros.classificacao = "profissional" AND (membros.registro_conselho IS NULL OR membros.registro_conselho = "" OR membros.registro_conselho = "0") AND o id da clinica atual (clinica_membros) esta contido na lista membros.incluir, que usa "&" como separador). Quando a LLM/ChatGPT Simulator responde a um paciente via WhatsApp e menciona que ira consultar o medico ou a secretaria, este campo e atualizado automaticamente pelo acompanhamento_whatsapp.py para que o sistema de notificacoes (chatgpt_integracao_criado_pelo_gemini.js.php) alerte o usuario correto, exibindo o chat completo para que ele responda diretamente ao paciente.'
  AFTER `mensagens`;

-- 2) Add index for fast lookup of pending notifications
ALTER TABLE `chatgpt_chats`
  ADD INDEX IF NOT EXISTS `idx_notificacao_pendente` (`notificacao_pendente`);
