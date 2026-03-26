-- Migration: Create dedicated WhatsApp contact cache table
-- Date: 2026-03-26
-- Purpose:
--   Store canonical WhatsApp contact identity data (phone + names shown in
--   WhatsApp Web sidebar / contact info panel) so the follow-up monitor can
--   resolve chats that are displayed by personal name instead of explicit phone.
--
-- Design notes for LLMs/agents:
--   1) This table is NOT the conversation transcript. Transcript remains in
--      chatgpt_chats.mensagens (chat_mode='whatsapp').
--   2) This table is an identity/index layer used to map:
--      WhatsApp chat title -> normalized phone -> patient/atendimento.
--   3) Rows are updated over time (upsert). Keep the latest known profile data
--      in each row and track first/last seen timestamps.

CREATE TABLE IF NOT EXISTS `chatgpt_whatsapp` (
  -- Surrogate key. Internal only; never sent to WhatsApp.
  `id` bigint unsigned NOT NULL AUTO_INCREMENT
    COMMENT 'Primary key interna da tabela de cache de contatos WhatsApp. Usada apenas para ordenação e auditoria local.',

  -- Canonical phone in normalized E.164-like digits-only format.
  -- Example (Brazil): 5581981487277
  `whatsapp_phone` varchar(20) NOT NULL
    COMMENT 'Telefone WhatsApp normalizado (somente dígitos, geralmente padrão E.164 sem símbolo +). Ex.: 5581981487277. É a chave técnica principal para reconciliar conversa e paciente.',

  -- Name visible in the chat list/header (may be personal name or phone text).
  `wa_display_name` varchar(255) DEFAULT NULL
    COMMENT 'Nome/título exibido no chat do WhatsApp Web no momento da captura (sidebar/header). Pode ser nome salvo pelo usuário, nome de perfil, ou o próprio número se não houver contato nomeado.',

  -- Name shown in contact details panel. Can differ from display name.
  `wa_profile_name` varchar(255) DEFAULT NULL
    COMMENT 'Nome observado no painel "Dados do contato" do WhatsApp Web (quando disponível). Pode diferir do título da sidebar e serve como segundo sinal de identidade.',

  -- Raw chat title that triggered capture (useful for debugging selector behavior).
  `wa_chat_title` varchar(255) DEFAULT NULL
    COMMENT 'Título bruto do chat que originou a captura (campo diagnóstico). Ajuda a depurar cenários em que o WhatsApp troca o rótulo exibido.',

  -- Optional relational bridge to local business entities.
  `id_paciente` int(10) DEFAULT NULL
    COMMENT 'FK lógica para membros.id do paciente relacionado a este contato. Pode ser NULL quando o sistema ainda não conseguiu correlacionar o contato ao cadastro interno.',

  `id_atendimento` int(10) DEFAULT NULL
    COMMENT 'FK lógica para atendimento (chatgpt_atendimentos_analise.id_atendimento) mais recente associado ao contato. Pode ser NULL até haver correlação segura.',

  -- Semantic signal for matching logic.
  `is_named_contact` tinyint(1) NOT NULL DEFAULT '0'
    COMMENT 'Flag semântica: 1 quando o título exibido aparenta nome próprio (não apenas número), 0 caso contrário. Útil para priorizar reconciliação por dados de contato.',

  -- Flexible payload for future WhatsApp metadata without schema changes.
  `profile_payload_json` longtext DEFAULT NULL
    COMMENT 'Payload JSON bruto da captura (snapshot). Estrutura livre para evolução (ex.: source, captured_at_utc, display_name, profile_name, chat_title). Permite extensões sem migration imediata.',

  -- Provenance: where this snapshot came from in the pipeline.
  `source` varchar(50) NOT NULL DEFAULT 'unknown'
    COMMENT 'Origem da captura do contato. Exemplos: send_followup (após envio), monitor_incoming (monitor de respostas), manual_sync. Ajuda em auditoria operacional.',

  -- Lifecycle timestamps.
  `first_seen_at` datetime DEFAULT NULL
    COMMENT 'Primeiro momento UTC em que este contato foi observado pelo sistema nesta tabela.',

  `last_seen_at` datetime DEFAULT NULL
    COMMENT 'Último momento UTC em que este contato foi revalidado/capturado no WhatsApp Web.',

  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP
    COMMENT 'Timestamp de criação física da linha no banco (gerido pelo MySQL).',

  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    COMMENT 'Timestamp de última atualização física da linha (gerido pelo MySQL).',

  PRIMARY KEY (`id`),

  -- One canonical row per normalized phone number.
  UNIQUE KEY `uq_chatgpt_whatsapp_phone` (`whatsapp_phone`),

  -- Query acceleration for monitor/reconciliation routines.
  KEY `idx_chatgpt_whatsapp_display_name` (`wa_display_name`),
  KEY `idx_chatgpt_whatsapp_profile_name` (`wa_profile_name`),
  KEY `idx_chatgpt_whatsapp_patient` (`id_paciente`),
  KEY `idx_chatgpt_whatsapp_atendimento` (`id_atendimento`),
  KEY `idx_chatgpt_whatsapp_last_seen` (`last_seen_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='Cache operacional de identidade de contatos WhatsApp para reconciliar chats nomeados com paciente/telefone internos.';
