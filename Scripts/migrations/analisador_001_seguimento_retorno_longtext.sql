-- Migração one-shot: amplia campo de seguimento para JSON completo
ALTER TABLE __TABELA__
MODIFY COLUMN seguimento_retorno_estimado LONGTEXT NULL
COMMENT 'JSON completo do objeto seguimento_retorno_estimado retornado pelo LLM.';
