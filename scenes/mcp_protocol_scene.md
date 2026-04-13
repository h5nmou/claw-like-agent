# Scene: MCP Self-Validation & Schema Update Protocol

## 역할 (Role)
MCP 서버를 호출하거나 MCP 기반 스킬을 생성할 때, **반드시 아래 프로토콜을 준수**하라.

---

## 1. 명세 우선 원칙 (Schema-First)

도구를 호출하거나 스킬 코드를 작성하기 전, **`tools/list`로 실제 inputSchema를 먼저 확인**하라.

- MCP 서버가 반환한 `tools/list` 결과를 **항상 최신 명세**로 간주한다.
- 과거 경험이나 추측으로 파라미터 형식을 결정하지 마라.
- 스킬 코드를 생성할 때도 probe 단계에서 얻은 `tools/list` 스키마를 코드에 반영하라.

---

## 2. 데이터 타입 엄수 (Strict Type Compliance)

서버 `inputSchema`가 요구하는 타입을 **절대 임의로 변환하지 마라**.

| 서버가 요구하는 타입 | 절대 금지 |
|---|---|
| `object` `{latitude, longitude}` | ❌ `"33.41, 126.39"` 문자열로 보내기 |
| `number` | ❌ `"4.5"` 문자열로 보내기 |
| `array` | ❌ 단일 문자열로 보내기 |
| `string` | ❌ 객체/숫자로 보내기 |

### 좌표 타입 예시 (Google Maps MCP)
```python
# ✅ 올바른 예 — 서버가 object를 요구하는 경우
"location": {"latitude": 33.410571, "longitude": 126.393147}

# ❌ 잘못된 예 — 문자열 전달
"location": "33.410571,126.393147"

# ❌ 잘못된 예 — 한글 주소 전달 (geocoding 불필요)
"location": "제주도 애월읍"
```

---

## 3. 에러 복구 프로토콜 (Error Recovery)

도구 호출 중 다음 에러가 발생하면 **즉시 명세 재확인 후 코드 수정**하라:

- `Method not found`
- `Invalid params`
- `위경도로 변환할 수 없습니다`
- `Unknown parameter`
- `Required property missing`

### 복구 절차
```
STEP 1: tools/list 재호출 → 현재 서버의 실제 inputSchema 확인
STEP 2: 에러 원인 파악 (타입 불일치? 파라미터명 오류? 구조 불일치?)
STEP 3: 스킬 코드에서 해당 파라미터를 명세에 맞게 수정
STEP 4: 수정된 코드로 재시도
STEP 5: 사장님에게 보고:
        "서버 명세를 확인하여 [변경 내용]을 수정했습니다."
```

---

## 4. 숙소 좌표 활용 원칙

이벤트 페이로드에 `lat`, `lng` 또는 `property_lat`, `property_lng`가 포함된 경우:

- **위경도 변환(geocoding)을 시도하지 마라** — 이미 정확한 좌표가 제공된 것이다.
- 해당 값을 스킬 파라미터에 **그대로** 전달하라.
- 주소 문자열("제주도 애월읍")로 변환하거나 geocode API를 별도 호출하지 마라.

```python
# 이벤트에서 좌표를 직접 사용하는 올바른 방법
latitude  = event.get("lat") or event.get("property_lat")   # 33.410571
longitude = event.get("lng") or event.get("property_lng")  # 126.393147
```

---

## 5. 증거 제시 의무

명세 확인 후 수정했을 때는 반드시 사장님에게 아래 형식으로 보고하라:

```
✅ [MCP 명세 검증 완료]
- 확인한 도구: maps_search_places
- 발견된 불일치: location 파라미터가 문자열이 아닌 {latitude, longitude} 객체를 요구
- 수정 내용: 문자열 "제주도 애월읍" → {"latitude": 33.410571, "longitude": 126.393147}
- 재시도 결과: ✅ 성공
```

---

## 6. No-Guessing Policy (추측 금지 원칙)

스킬 코드를 작성하기 전, **반드시 대상 MCP의 `tools/list`를 호출**하여 파라미터의 `type`과 `properties`를 완벽히 분석하라.

- 과거 경험이나 문서에서 본 예시를 그대로 쓰지 마라 — 서버 버전에 따라 다를 수 있다.
- `tools/list`로 확인한 실제 스키마를 코드에 반영하라.
- 스키마 확인 없이 파라미터명이나 타입을 추측하면 `Invalid params` 오류가 발생한다.

```
PROBE 절차:
STEP 1: subprocess로 MCP 서버 시작
STEP 2: initialize → notifications/initialized 핸드셰이크
STEP 3: tools/list 호출 → 각 도구의 inputSchema 확인
STEP 4: 확인된 스키마를 스킬 코드에 정확히 반영
```

---

## 7. Type-Resilient Logic (타입 방어 코드)

위치(Location), 시간(Time)처럼 **다양한 형식으로 전달될 수 있는 파라미터**는 반드시 if/else로 string과 object 타입을 모두 처리하는 방어적 코드를 작성하라.

```python
# ✅ 방어적 코드 — string, dict, 또는 별도 float 파라미터 모두 처리
if latitude is not None and longitude is not None:
    # 이미 좌표가 제공됨 — geocoding 불필요
    location_param = {"latitude": float(latitude), "longitude": float(longitude)}
elif isinstance(location, dict):
    lat = location.get("lat") or location.get("latitude")
    lng = location.get("lng") or location.get("longitude")
    location_param = {"latitude": float(lat), "longitude": float(lng)}
elif isinstance(location, str):
    # geocoding 수행 (maps_geocode 호출)
    ...
else:
    return {"error": "location 파라미터 형식을 알 수 없습니다"}

# ❌ 금지 — 단일 타입만 가정
location_param = {"latitude": float(location["lat"]), ...}  # dict만 가정
```

MCP 응답에서 좌표 추출 시도 순서:
1. `result["location"]["lat"]` / `result["location"]["lng"]`
2. `result["lat"]` / `result["lng"]`
3. `result["geometry"]["location"]["lat"]` (Google Maps REST 구조)
