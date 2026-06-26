"""
LLM 分析引擎 - 支持 LM Studio / Ollama / OpenAI

优先级：LM Studio > Ollama > OpenAI
推荐：LM Studio（图形界面友好，OpenAI兼容API）
"""
import os
import json
import requests


# ============ 配置（按优先级排列）============

# --- 方案1：LM Studio（推荐，Windows 主机部署）---
# LM Studio 默认地址，Mac 访问时改为 Windows 的 IP
LM_STUDIO_URL = os.environ.get("LM_STUDIO_URL", "http://localhost:1234")
LM_STUDIO_MODEL = os.environ.get("LM_STUDIO_MODEL", "")  # 留空则使用 LM Studio 当前加载的模型

# --- 方案2：Ollama ---
OLLAMA_BASE_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")

# --- 方案3：OpenAI（云端备选）---
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

# 选择使用哪个后端：lmstudio / ollama / openai / auto（自动检测）
LLM_BACKEND = os.environ.get("LLM_BACKEND", "auto")


def _call_lm_studio(prompt: str, model: str = None) -> str:
    """
    调用 LM Studio（OpenAI 兼容 API）
    LM Studio 默认运行在 http://localhost:1234/v1
    无需 API Key，或随意填写如 "lm-studio"
    """
    url = f"{LM_STUDIO_URL}/v1/chat/completions"
    headers = {
        "Authorization": "Bearer lm-studio",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model or LM_STUDIO_MODEL or "local-model",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 2048,
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=180)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except requests.exceptions.ConnectionError:
        return "[ERROR_LM_STUDIO]"
    except Exception as e:
        return f"[ERROR_LM_STUDIO] {str(e)}"


def _call_ollama(prompt: str, model: str = None) -> str:
    """调用 Ollama 本地模型"""
    url = f"{OLLAMA_BASE_URL}/api/generate"
    payload = {
        "model": model or OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.3, "num_predict": 2048},
    }
    try:
        resp = requests.post(url, json=payload, timeout=180)
        resp.raise_for_status()
        return resp.json().get("response", "")
    except requests.exceptions.ConnectionError:
        return "[ERROR_OLLAMA]"
    except Exception as e:
        return f"[ERROR_OLLAMA] {str(e)}"


def _call_openai(prompt: str, model: str = None) -> str:
    """调用 OpenAI API"""
    if not OPENAI_API_KEY:
        return "[ERROR_OPENAI] 未配置 OPENAI_API_KEY"
    url = f"{OPENAI_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model or OPENAI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 2048,
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"[ERROR_OPENAI] {str(e)}"


def ask_llm(prompt: str) -> str:
    """
    统一调用入口

    后端选择逻辑：
    - LLM_BACKEND=lmstudio → 只用 LM Studio
    - LLM_BACKEND=ollama   → 只用 Ollama
    - LLM_BACKEND=openai   → 只用 OpenAI
    - LLM_BACKEND=auto     → 自动尝试：LM Studio → Ollama → OpenAI
    """
    if LLM_BACKEND == "lmstudio":
        result = _call_lm_studio(prompt)
        if not result.startswith("[ERROR"):
            return result
        return result.replace("[ERROR_LM_STUDIO]", "[错误] 无法连接到 LM Studio。请确认 LM Studio 已启动并加载了模型。")

    if LLM_BACKEND == "ollama":
        result = _call_ollama(prompt)
        if not result.startswith("[ERROR"):
            return result
        return result.replace("[ERROR_OLLAMA]", "[错误] 无法连接到 Ollama。请确认 Ollama 已启动。")

    if LLM_BACKEND == "openai":
        return _call_openai(prompt)

    # === auto 模式：自动尝试所有后端 ===
    # 1. 先尝试 LM Studio
    result = _call_lm_studio(prompt)
    if not result.startswith("[ERROR"):
        return result

    # 2. 回退到 Ollama
    result = _call_ollama(prompt)
    if not result.startswith("[ERROR"):
        return result

    # 3. 最后尝试 OpenAI
    if OPENAI_API_KEY:
        return _call_openai(prompt)

    return (
        "[错误] 所有 LLM 后端均不可用。\n\n"
        "请至少配置以下之一：\n"
        "• **LM Studio**（推荐）：在 Windows 上启动 LM Studio 并加载模型\n"
        "• **Ollama**：运行 ollama serve 并拉取模型\n"
        "• **OpenAI**：设置环境变量 OPENAI_API_KEY\n\n"
        "提示：在 Mac 上访问 Windows 上的 LM Studio，\n"
        "需要将 LM_STUDIO_URL 设为 http://Windows的IP:1234"
    )


# ==================== 业务分析 Prompt ====================

SYSTEM_CONTEXT = """你是一位资深的B2B客户情报分析师，擅长从零散信息中挖掘客户痛点和需求。
你的输出要求：
- 简洁、精准、可执行
- 使用中文
- 用结构化的方式呈现
- 不说废话，直接给结论"""


def analyze_customer(customer_info: dict, follow_ups: list, intelligences: list) -> str:
    """综合分析一个客户：生成痛点诊断、需求画像、行动建议"""
    context_parts = []
    context_parts.append(f"## 客户基础信息")
    context_parts.append(f"公司: {customer_info.get('company', '未知')}")
    context_parts.append(f"联系人: {customer_info.get('name', '未知')}")
    context_parts.append(f"国家: {customer_info.get('country', '未知')}")
    context_parts.append(
        f"行业/领域: {customer_info.get('field', '未知') or customer_info.get('industry', '未知')}"
    )
    context_parts.append(f"客户分级: {customer_info.get('level', '未知')}")
    context_parts.append(
        f"类型: {customer_info.get('type', '未知') or customer_info.get('customer_type', '未知')}"
    )
    context_parts.append(f"公司规模: {customer_info.get('company_size', '未知')}")
    if customer_info.get("profile"):
        context_parts.append(f"简介: {customer_info['profile']}")
    if customer_info.get("notes"):
        context_parts.append(f"备注: {customer_info['notes']}")

    if follow_ups:
        context_parts.append("\n## 历史沟通记录")
        for r in follow_ups[:20]:
            date_str = r.get("follow_date", "")[:10]
            source = r.get("source", "")
            content = r.get("content", "")[:300]
            result = r.get("result", "")
            next_p = r.get("next_plan", "")
            entry = f"\n[{date_str}] ({source})\n沟通内容: {content}"
            if result:
                entry += f"\n结果: {result}"
            if next_p:
                entry += f"\n下一步: {next_p}"
            context_parts.append(entry)

    if intelligences:
        context_parts.append("\n## 已有调研情报")
        for intel in intelligences[:10]:
            raw = intel.get("raw_input", "")[:200]
            findings = intel.get("key_findings", "")[:200]
            needs = intel.get("needs_analysis", "")[:200]
            entry = f"\n[情报] 原始信息: {raw}"
            if findings:
                entry += f"\n关键发现: {findings}"
            if needs:
                entry += f"\n需求分析: {needs}"
            context_parts.append(entry)

    full_context = "\n".join(context_parts)

    prompt = f"""{SYSTEM_CONTEXT}

请基于以下客户信息进行深度分析：

{full_context}

请严格按照以下格式输出（每个部分都要填写）：

---

## 一、客户画像总结
用2-3句话概括这个客户是谁、做什么的、目前处于什么阶段。

## 二、已识别的痛点
列出1-3个明确的痛点，每个痛点标注：
- 痛点描述（一句话）
- 严重程度：高/中/低
- 来源依据（从哪条信息推断的）

## 三、潜在需求分析
基于痛点推导出的产品/服务需求：
1. **核心需求**：（最可能成交的点）
2. **延伸需求**：（未来可能需要的）
3. **隐性需求**：（客户没明说但可能存在的）

## 四、竞争优势与风险
**我们的优势**：
**竞争风险**：
**决策关键人态度推测**：

## 五、下一步行动建议
给出3条具体、可执行的行动建议，按优先级排列：
1. 【高优先级】...
2. 【中优先级】...
3. 【低优先级】...

---

请开始分析："""

    return ask_llm(prompt)


def analyze_single_intelligence(raw_text: str, customer_name: str = "") -> dict:
    """分析单条新录入的情报，自动提取结构化信息"""
    prompt = f"""{SYSTEM_CONTEXT}

以下是一条关于客户 "{customer_name}" 的新情报/观察记录：

{raw_text}

请分析并严格按以下JSON格式输出（不要输出其他内容）：
{{
    "summary": "用一句话总结这条情报的核心内容",
    "key_findings": "从中提取的关键发现（2-3点）",
    "pain_points": "这条情报暗示的客户痛点",
    "opportunity": "这条情报中蕴含的商业机会",
    "suggested_action": "基于此情报建议的下一步行动"
}}

请开始分析："""

    raw_result = ask_llm(prompt)

    try:
        start = raw_result.find("{")
        end = raw_result.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(raw_result[start:end])
    except (json.JSONDecodeError, ValueError):
        pass

    return {"raw_analysis": raw_result}


def quick_chat(question: str, customer_context: str = "") -> str:
    """快速问答模式 - 针对客户的自由提问"""
    context_section = ""
    if customer_context:
        context_section = f"\n\n参考的客户背景信息：\n{customer_context}\n"

    prompt = f"""{SYSTEM_CONTEXT}
{context_section}
用户问题：{question}

请简洁、专业地回答。"""

    return ask_llm(prompt)
