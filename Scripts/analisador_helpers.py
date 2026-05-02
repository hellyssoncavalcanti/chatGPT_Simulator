"""Helpers puros de analisador_prontuarios.py.

Módulo puro: sem Flask, Playwright nem config.

Extraído para permitir testes offline das funções de processamento:
grafo clínico, normalização de erros esgotados, serialização compacta
de valores compilados, resumo por paciente, strip HTML, detecção de
erros de conexão com a LLM.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from html.parser import HTMLParser
import html as _html_mod
from typing import Any

try:
    import requests as _requests
except ImportError:  # pragma: no cover
    _requests = None


# ─── Serialização compacta ────────────────────────────────────────────────────

def stringify_compact(value: Any) -> str:
    """Serializa valor de campo compilado em string legível/compacta.

    - list  → itens não-nulos unidos por "; "
    - dict  → json.dumps
    - str   → str stripped
    - None  → ""
    """
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except Exception:
        parsed = value
    if isinstance(parsed, list):
        partes = []
        for item in parsed:
            if item in (None, "", [], {}):
                continue
            partes.append(
                json.dumps(item, ensure_ascii=False)
                if isinstance(item, (dict, list))
                else str(item)
            )
        return "; ".join(partes)
    if isinstance(parsed, dict):
        return json.dumps(parsed, ensure_ascii=False)
    return str(parsed or "").strip()


def format_compiled_value_for_prompt(value: Any, max_chars: int = 1200) -> Any:
    """Prepara valor compilado para inserção no prompt da LLM.

    - str → normaliza espaços e trunca em ``max_chars``
    - outros tipos → retorna sem alteração (list/dict para a LLM processar)
    """
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except Exception:
        parsed = value
    if isinstance(parsed, str):
        texto = re.sub(r"\s+", " ", parsed).strip()
        return texto[:max_chars]
    return parsed


# ─── Razões de esgotamento ────────────────────────────────────────────────────

def normalize_esgotado_reason(erro_msg: str) -> str:
    """Extrai motivo legível/agrupável do campo ``erro_msg`` de um registro esgotado."""
    texto = re.sub(r"\s+", " ", str(erro_msg or "")).strip(" |")
    if not texto:
        return "Sem mensagem de erro registrada"

    partes = [p.strip() for p in texto.split("|") if p.strip()]
    partes_validas = [
        p for p in partes
        if not p.startswith("[AUTO-RESET")
        and not p.startswith("[AUTO-RESET-STARTUP")
    ]
    motivo = (
        partes_validas[-1] if partes_validas
        else partes[-1] if partes
        else texto
    ).strip()
    motivo = re.sub(r"\s+", " ", motivo)
    motivo_lower = motivo.lower()

    if "texto insuficiente após remoção de html" in motivo_lower:
        return "Prontuário ficou insuficiente após limpeza/remoção de HTML."

    if (
        "simulador não retornou conteúdo markdown" in motivo_lower
        or "llm não retornou conteúdo markdown" in motivo_lower
    ):
        return "LLM não retornou resposta final em markdown utilizável."

    if (
        "llm não retornou json válido" in motivo_lower
        or "llm não retornou bloco json" in motivo_lower
        or "expecting ',' delimiter" in motivo_lower
        or "expecting property name enclosed in double quotes" in motivo_lower
        or "unterminated string" in motivo_lower
        or "invalid control character" in motivo_lower
        or "extra data" in motivo_lower
    ):
        return "LLM retornou JSON inválido/malformado para o schema esperado."

    if "api_exec recusou" in motivo_lower or "execute_sql recusou" in motivo_lower:
        return "Falha de persistência/execução SQL ao salvar ou consultar a análise."

    if "simulador retornou erro" in motivo_lower:
        return "ChatGPT Simulator retornou erro durante a análise."

    return motivo[:180] + ("..." if len(motivo) > 180 else "")


def group_esgotado_reasons(rows: list[dict]) -> list[dict]:
    """Agrupa e conta motivos de esgotamento, retornando os 5 mais frequentes."""
    contador: Counter = Counter()
    for row in rows or []:
        motivo = normalize_esgotado_reason((row or {}).get("erro_msg"))
        contador[motivo] += 1
    return [
        {"motivo": motivo, "total": total}
        for motivo, total in contador.most_common(5)
    ]


# ─── Resumo de paciente ───────────────────────────────────────────────────────

def montar_resumo_fallback(
    maior_resumo: str,
    dt_consulta: str,
    texto_consulta: str,
) -> str:
    """Monta resumo_fallback sem duplicar consulta do mesmo datetime.

    - Se ``maior_resumo`` já contém a linha "Consulta de <dt_consulta>: ..."
      com o mesmo conteúdo → retorna ``maior_resumo`` inalterado.
    - Se contém a mesma data mas com conteúdo diferente → substitui a linha.
    - Se não contém → concatena no final.
    """
    sufixo = f"Consulta de {dt_consulta}: {texto_consulta}".strip()

    if not maior_resumo:
        return sufixo

    if not dt_consulta:
        return f"{maior_resumo}\n{sufixo}".strip()

    prefixo_dt = f"Consulta de {dt_consulta}:"
    prefixo_dt_lower = prefixo_dt.lower()
    linhas = maior_resumo.split("\n")
    idx_encontrado = None

    for idx, linha in enumerate(linhas):
        if linha.strip().lower().startswith(prefixo_dt_lower):
            idx_encontrado = idx
            break

    if idx_encontrado is None:
        return f"{maior_resumo}\n{sufixo}".strip()

    conteudo_existente = linhas[idx_encontrado].strip()[len(prefixo_dt):].strip()
    if conteudo_existente == texto_consulta.strip():
        return maior_resumo

    linhas[idx_encontrado] = sufixo
    return "\n".join(linhas).strip()


# ─── Grafo clínico ────────────────────────────────────────────────────────────

_TIPO_ALIAS: dict[str, str] = {
    "patient": "paciente",
    "paciente": "paciente",
    "patient_name": "paciente",
    "diagnosis": "diagnostico",
    "diagnostico": "diagnostico",
    "diagnóstico": "diagnostico",
    "cid": "diagnostico",
    "symptom": "sintoma",
    "symptoms": "sintoma",
    "sintoma": "sintoma",
    "sinal": "sintoma",
    "sign": "sintoma",
    "medication": "medicamento",
    "medicine": "medicamento",
    "drug": "medicamento",
    "medicamento": "medicamento",
    "medicacao": "medicamento",
    "medicação": "medicamento",
    "therapy": "terapia",
    "terapia": "terapia",
    "exam": "exame",
    "test": "exame",
    "exame": "exame",
    "gene": "gene",
    "genetics": "gene",
    "genetica": "gene",
    "genética": "gene",
    "behavior": "comportamento",
    "behaviour": "comportamento",
    "comportamento": "comportamento",
    "conduct": "conduta",
    "plan": "conduta",
    "conduta": "conduta",
    "risk": "risco",
    "risco": "risco",
    "pending": "pendencia",
    "pendencia": "pendencia",
    "pendência": "pendencia",
}

_CAMPOS_BASE_NODE = {
    "id", "node_id", "tipo", "node_tipo", "type", "category",
    "valor", "node_valor", "value", "label", "name", "nome",
    "normalizado", "node_normalizado", "normalized", "normalised",
    "contexto", "node_contexto", "context", "description", "descricao",
}


def normalizar_node(nd: dict) -> dict:
    """Normaliza campos de um node do grafo clínico para o formato PHP."""
    tipo_bruto = str(
        nd.get("tipo") or nd.get("node_tipo") or nd.get("type")
        or nd.get("category") or ""
    ).strip()
    valor = str(
        nd.get("valor") or nd.get("node_valor") or nd.get("value")
        or nd.get("label") or nd.get("name") or nd.get("nome") or ""
    ).strip()
    normalizado = str(
        nd.get("normalizado") or nd.get("node_normalizado")
        or nd.get("normalized") or nd.get("normalised") or ""
    ).strip()
    contexto = str(
        nd.get("contexto") or nd.get("node_contexto") or nd.get("context")
        or nd.get("description") or nd.get("descricao") or ""
    ).strip()

    tipo = _TIPO_ALIAS.get(tipo_bruto.lower(), tipo_bruto.lower())

    if not normalizado and valor:
        normalizado = re.sub(r"[^a-z0-9]+", "_", valor.lower()).strip("_")

    extras = []
    for k, v in nd.items():
        if k in _CAMPOS_BASE_NODE or v in (None, "", [], {}):
            continue
        if isinstance(v, (dict, list)):
            extras.append(f"{k}={json.dumps(v, ensure_ascii=False)}")
        else:
            extras.append(f"{k}={v}")
    extras_txt = " | ".join(extras[:4])
    if extras_txt:
        contexto = (
            f"{contexto} | {extras_txt}".strip(" |") if contexto else extras_txt
        )

    node_id = str(nd.get("id") or nd.get("node_id") or "").strip()
    if not node_id and tipo and normalizado:
        node_id = f"{tipo}_{normalizado[:80]}"

    return {
        "id": node_id,
        "tipo": tipo,
        "valor": valor,
        "normalizado": normalizado,
        "contexto": contexto,
    }


def normalizar_edge(ed: dict) -> dict:
    """Normaliza campos de uma edge do grafo clínico para o formato PHP."""
    return {
        "node_origem": (
            ed.get("node_origem") or ed.get("source") or ed.get("from")
            or ed.get("origem") or ""
        ),
        "node_destino": (
            ed.get("node_destino") or ed.get("target") or ed.get("to")
            or ed.get("destino") or ""
        ),
        "relacao_tipo": (
            ed.get("relacao_tipo") or ed.get("relation") or ed.get("type")
            or ed.get("tipo") or ed.get("relationship") or ""
        ),
        "relacao_contexto": (
            ed.get("relacao_contexto") or ed.get("contexto") or ed.get("context")
            or ed.get("description") or ed.get("descricao") or ""
        ),
    }


def deduplicar_nodes_grafo(nodes: list) -> list:
    """Deduplica nodes do grafo pela chave (tipo, normalizado/valor)."""
    dedup: dict = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        chave = (
            str(node.get("tipo") or "").strip().lower(),
            str(node.get("normalizado") or node.get("valor") or "").strip().lower(),
        )
        if not chave[1]:
            continue
        if chave not in dedup:
            dedup[chave] = node
            continue
        existente = dedup[chave]
        if not existente.get("id") and node.get("id"):
            existente["id"] = node["id"]
        if not existente.get("contexto") and node.get("contexto"):
            existente["contexto"] = node["contexto"]
        elif node.get("contexto") and node["contexto"] not in str(existente.get("contexto") or ""):
            existente["contexto"] = (
                f"{existente.get('contexto', '')} | {node['contexto']}".strip(" |")
            )
    return list(dedup.values())


def primeiro_node_representativo(nodes: list):
    """Retorna o node mais representativo do grafo por ordem de prioridade clínica."""
    for tipo_prio in ("diagnostico", "medicamento", "terapia", "sintoma", "gene", "risco"):
        for node in nodes:
            if (node.get("tipo") or "").lower() == tipo_prio:
                return node
    return nodes[0] if nodes else None


def ensure_list(val: Any) -> list:
    """Normaliza para lista: None→[], JSON string de lista→lista, outros→[]."""
    if val is None:
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def is_grafo_generico(resultado: dict) -> bool:
    """Retorna True se o grafo clínico não tem nós relevantes suficientes (< 2)."""
    nodes = ensure_list(
        resultado.get("grafo_clinico_nodes")
        or resultado.get("grafo_nodes")
        or resultado.get("nodes")
    )
    if not nodes:
        return True
    tipos_relevantes = {
        "diagnostico", "sintoma", "medicamento", "terapia",
        "exame", "pendencia", "risco", "gene", "comportamento",
    }
    nodes_relevantes = [
        n for n in nodes
        if isinstance(n, dict)
        and (n.get("valor") or n.get("node_valor"))
        and ((n.get("tipo") or n.get("node_tipo") or "").lower() in tipos_relevantes)
    ]
    return len(nodes_relevantes) < 2


# ─── HTML ─────────────────────────────────────────────────────────────────────

class _StripHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)


def strip_html(raw: str) -> str:
    """Remove tags HTML e normaliza espaços excessivos."""
    p = _StripHTML()
    p.feed(_html_mod.unescape(raw or ""))
    return re.sub(r"\s{3,}", "\n\n", " ".join(p._parts)).strip()


# ─── Conexão LLM ──────────────────────────────────────────────────────────────

def is_llm_connection_error(exc: BaseException) -> bool:
    """Detecta erros transitórios de conexão/stream com o ChatGPT Simulator."""
    if isinstance(exc, (ConnectionResetError, BrokenPipeError, TimeoutError)):
        return True
    if _requests is not None:
        if isinstance(exc, (_requests.Timeout, _requests.ConnectionError)):
            return True
    texto = str(exc or "").lower()
    padroes = (
        "connection reset",
        "connection aborted",
        "connection broken",
        "remote end closed connection",
        "forçado o cancelamento de uma conexão existente pelo host remoto",
        "max retries exceeded",
        "failed to establish a new connection",
    )
    return any(p in texto for p in padroes)


__all__ = [
    "stringify_compact",
    "format_compiled_value_for_prompt",
    "normalize_esgotado_reason",
    "group_esgotado_reasons",
    "montar_resumo_fallback",
    "normalizar_node",
    "normalizar_edge",
    "deduplicar_nodes_grafo",
    "primeiro_node_representativo",
    "ensure_list",
    "is_grafo_generico",
    "strip_html",
    "is_llm_connection_error",
]
