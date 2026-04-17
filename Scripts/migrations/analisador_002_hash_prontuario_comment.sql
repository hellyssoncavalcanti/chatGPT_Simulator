-- Migração one-shot: normaliza comentário do hash_prontuario
ALTER TABLE __TABELA__
MODIFY COLUMN hash_prontuario CHAR(64) NULL
COMMENT 'Hash SHA-256 do conteudo bruto do prontuario analisado.';
