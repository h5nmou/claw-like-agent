"""
mcp_client.py — Phone-MCP 연동 클라이언트

에이전트 구동 시 지정된 MCP 서버(Phone)에 접속하여 사용 가능한 도구 목록을 가져오고,
이를 엔진의 Tool Registry에 동적으로 등록합니다.
"""

import logging
import inspect
import httpx
from typing import Any, List, Dict
from core.executor import _TOOL_REGISTRY

logger = logging.getLogger("mcp_client")

class MCPClient:
    def __init__(self, server_url: str):
        # /mcp 경로가 없으면 자동으로 추가 (Phone-MCP 기본 스펙 규격 맞춤)
        base = server_url.rstrip("/")
        self.server_url = base if base.endswith("/mcp") else f"{base}/mcp"
        self.client = httpx.AsyncClient(timeout=10.0)

    async def fetch_and_register_tools(self):
        """MCP 서버로부터 도구 목록을 가져와 등록."""
        try:
            logger.info(f"Connecting to Phone-MCP at {self.server_url}...")
            
            # JSON-RPC listTools 요청
            payload = {
                "jsonrpc": "2.0",
                "method": "tools/list",
                "params": {},
                "id": 1
            }
            
            response = await self.client.post(self.server_url, json=payload)
            
            if response.status_code != 200:
                logger.error(f"Failed to fetch MCP tools: HTTP {response.status_code}")
                return

            data = response.json()
            if "result" in data and "tools" in data["result"]:
                tools = data["result"]["tools"]
                logger.info(f"Found {len(tools)} tools from Phone-MCP.")
                
                for tool_info in tools:
                    self._register_mcp_tool(tool_info)
            else:
                logger.warning(f"No tools found in MCP response: {data}")

        except Exception as e:
            logger.error(f"Error connecting to Phone-MCP: {str(e)}")

    def _register_mcp_tool(self, tool_info: Dict[str, Any]):
        """원격 MCP 도구를 로컬 레지스트리에 등록."""
        name = tool_info["name"]
        description = tool_info.get("description", "")
        input_schema = tool_info.get("inputSchema", {})

        # MCP 도구를 실행하는 원격 호출 함수 생성
        async def mcp_wrapper(**kwargs):
            logger.info(f"Calling Remote MCP Tool: {name} with args: {kwargs}")
            
            async def _do_call(call_kwargs):
                payload = {
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "params": {"name": name, "arguments": call_kwargs},
                    "id": 2
                }
                async with httpx.AsyncClient() as client:
                    resp = await client.post(self.server_url, json=payload)
                    return resp.json()

            try:
                resp_data = await _do_call(kwargs)
                
                # --- [Smart Auto-Spacer] ---
                # 응답 값이 비어있는지 텍스트로 확인
                def is_empty_result(r_data):
                    content_list = r_data.get("result", {}).get("content", [])
                    texts = [c.get("text", "") for c in content_list if c.get("type") == "text"]
                    combined = " ".join(texts)
                    return ('"total_results": 0' in combined or 
                            '"total_contacts":0' in combined or 
                            'No mapping found' in combined)

                # 한글 뒤에 영어/숫자가 붙어 있어서 검색이 안 된 경우, 띄어쓰기를 넣어서 백그라운드 재귀 조회
                if is_empty_result(resp_data):
                    import re
                    name_keys = ["contact_name", "name_filter", "name", "display_name", "target_name"]
                    modified = False
                    new_kwargs = kwargs.copy()
                    
                    for k in name_keys:
                        if k in new_kwargs and isinstance(new_kwargs[k], str):
                            orig_val = new_kwargs[k]
                            # "예약자A" -> "예약자 A", "고객1" -> "고객 1"
                            spaced_val = re.sub(r'([가-힣])([a-zA-Z0-9])', r'\1 \2', orig_val)
                            if spaced_val != orig_val:
                                new_kwargs[k] = spaced_val
                                modified = True
                                
                    if modified:
                        logger.info(f"Smart Auto-Spacer triggered! Retrying MCP Tool '{name}': {kwargs} -> {new_kwargs}")
                        retry_resp = await _do_call(new_kwargs)
                        if not is_empty_result(retry_resp):
                            resp_data = retry_resp  # 띄어쓰기 한 버전이 데이터가 있으면 그걸로 덮어씌움
                # -------------------------

                if "result" in resp_data:
                    # MCP 결과 내 content에서 text만 추출하여 반환 (에이전트 가독성용)
                    content_list = resp_data["result"].get("content", [])
                    texts = [c.get("text", "") for c in content_list if c.get("type") == "text"]
                    return "\n".join(texts) if texts else resp_data["result"]
                elif "error" in resp_data:
                    return {"error": resp_data["error"]}
                return resp_data
            except Exception as e:
                return {"error": f"Remote MCP call failed: {str(e)}"}

        # ── 시그니처 및 Docstring 동적 생성 ──
        
        # 설명 앞에 휴대전화 도구임을 명시하여 LLM의 인지 효율 향상
        display_description = f"[PHONE-MCP] {description}"
        
        properties = input_schema.get("properties", {})
        required_fields = input_schema.get("required", [])
        
        params = []
        doc_lines = [display_description, "", "Args:"]
        
        # JSON Schema 타입을 Python 타입 명칭으로 매핑 (Executor 파서 전용)
        type_map = {"string": "str", "number": "float", "integer": "int", "boolean": "bool", "array": "list", "object": "dict"}

        for pname, pinfo in properties.items():
            ptype_str = pinfo.get("type", "string")
            pdesc = pinfo.get("description", "")
            
            # inspect.Parameter 생성 (기본값 설정 여부로 필수 파라미터 구분)
            default_val = inspect.Parameter.empty if pname in required_fields else None
            params.append(inspect.Parameter(
                pname, 
                kind=inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=default_val
            ))
            
            # Docstring의 Args 섹션 작성 (executor.py의 파서가 읽음)
            doc_lines.append(f"    {pname} ({type_map.get(ptype_str, 'str')}): {pdesc}")

        # 메타데이터 주입
        mcp_wrapper.__name__ = name
        mcp_wrapper.__doc__ = "\n".join(doc_lines)
        
        # inspect.signature()를 오버라이드하여 Executor가 파라미터 목록을 인식하게 함
        mcp_wrapper.__signature__ = inspect.Signature(params)

        # 레지스트리에 등록
        _TOOL_REGISTRY[name] = mcp_wrapper
        logger.info(f"Registered Remote MCP Tool: {name} (parameters: {list(properties.keys())})")

    async def close(self):
        await self.client.aclose()
