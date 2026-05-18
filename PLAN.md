# KHDP 커넥터 아키텍처 계획

## 목적

KHDP 데이터 및 도구를 외부 AI 코딩 에이전트(Claude Code, Codex CLI, OpenCode, Cursor 등)에서 안전하고 일관되게 사용할 수 있도록 하는 커넥터 레이어를 설계한다. 특정 LLM 벤더에 종속되지 않으면서, KHDP의 인증·감사·접근통제 정책을 클라이언트 환경과 무관하게 서버 측에서 강제하는 것이 목표.

## 설계 원칙

1. **Vendor neutrality**: 의료 데이터 인프라는 다년(5–10년) 단위로 운영되는 반면 LLM 벤더 지형은 1–2년 단위로 변한다. 도구 인터페이스는 벤더 중립 표준(MCP)을 1차로 채택한다.
2. **Server-enforced security**: 인증·권한·감사 로그는 클라이언트가 무엇이든(LLM 추론이든 사람이든) 서버 측에서 강제된다. 클라이언트가 보안 프로토콜을 재구현하지 않는다.
3. **Reusable across surfaces**: 같은 백엔드를 MCP, CLI, 웹 UI, CI 환경에서 모두 동일하게 사용할 수 있어야 한다.
4. **Defense in depth**: manifest 기반 access control, AES-GCM URL 토큰, k-anonymity egress control 등 기존 KHDP 보안 레이어와 정합적으로 통합된다.
5. **Auditability over flexibility**: IRB·내부자 위협 모델에 부합하도록, 호출 경로의 자유도보다 감사 가능성을 우선한다.

## 3-tier 구조

```
┌─────────────────────────────────────────────────────────────┐
│ Tier 3: Vendor-specific 자산 (선택적)                          │
│   - Anthropic Skills (SKILL.md + 보조 스크립트)                │
│   - GPTs Custom Actions, Gemini Extensions 등                 │
└─────────────────────────────────────────────────────────────┘
                            ↓ 참조
┌─────────────────────────────────────────────────────────────┐
│ Tier 2: 벤더 중립 가이드 (KHDP_AGENT_GUIDE.md)                  │
│   - 도메인 워크플로우, 도구 사용 순서, 컨벤션                       │
│   - AGENTS.md / CLAUDE.md 형식으로 호환                         │
└─────────────────────────────────────────────────────────────┘
                            ↓ 호출
┌─────────────────────────────────────────────────────────────┐
│ Tier 1: KHDP MCP 서버 (1차 산출물)                              │
│   - 결정론적 도구 노출 (auth, dataset I/O, OMOP query)           │
│   - 인증/감사/권한 캡슐화                                         │
└─────────────────────────────────────────────────────────────┘
                            ↓ 호출
┌─────────────────────────────────────────────────────────────┐
│ KHDP Backend (snuh.ai)                                       │
│   - manifest registry, OMOP CDM, audit log, RID issuer       │
└─────────────────────────────────────────────────────────────┘
```

## Tier 1: KHDP MCP 서버

### 노출 도구 (초안)

**인증**
- `khdp_auth_status` — 현재 인증 상태와 사용자 컨텍스트 조회
- `khdp_auth_refresh` — refresh token 회전으로 세션 연장
- `khdp_auth_logout` — 토큰 폐기

> 로그인은 의도적으로 MCP 도구로 노출하지 않는다. PKCE Loopback
> 흐름은 브라우저 인터랙션이 필수이고, 사용자 자격 증명을 LLM 컨텍스트
> 밖에 두는 것이 보안 모델이다. 발급은 사용자가 터미널에서 `khdp login`
> 한 번 실행한 뒤, MCP 서버는 캐시된 토큰만 읽어 사용한다.

**데이터셋 I/O**
- `khdp_dataset_list` — 권한 있는 데이터셋 목록 조회 (manifest 기반)
- `khdp_dataset_describe` — 데이터셋 메타데이터·스키마·라이선스 조회
- `khdp_dataset_download` — 데이터셋 다운로드 (RID 기반 AES-GCM URL 발급)
- `khdp_dataset_upload` — 데이터셋 업로드 (PHI 스캔·manifest 등록 포함)

**OMOP CDM 분석**
- `khdp_omop_describe_table` — OMOP 테이블 스키마와 row 수
- `khdp_omop_find_concept` — concept_id ↔ 명칭 검색 (vocabulary 통합)
- `khdp_omop_sample_rows` — 테이블 샘플 (egress 정책 적용)
- `khdp_omop_query` — DuckDB 기반 read-only SQL 실행 (k-anonymity·row limit)

**감사·재현성**
- `khdp_audit_log_query` — 본인의 호출 이력 조회
- `khdp_result_pin` — IRB 재현성 요건 충족용 결과·쿼리·환경 스냅샷 저장

### 인증 모델

- **OAuth 2.0 + PKCE (loopback)** 채택. RFC 8252 권장 패턴.
- 서버 사이드 배포 시 `https://khdp.net/oauth/callback`, 로컬 MCP는 `http://127.0.0.1:<port>/callback`.
- Refresh token은 클라이언트 머신에 0600 퍼미션으로 저장, OS keychain 통합 옵션 검토.
- 모든 토큰은 사용자 단위. 다중 사용자 머신에서는 사용자별 격리.

### 감사 로그 통합

- 모든 MCP 도구 호출은 (user_id, tool, params_hash, timestamp, client_ua, result_status) 튜플로 immutable log에 기록.
- `params_hash`는 PHI를 평문 저장하지 않기 위해 정규화 후 해시.
- IRB 감사 시 `khdp_result_pin`으로 고정된 결과만 재현성 보장 대상.

### 기술 스택 (잠정)

- **언어**: Python 3.12 (KHDP 기존 인프라 호환)
- **MCP SDK**: 공식 Python SDK (`mcp` 패키지)
- **데이터 액세스**: DuckDB (read-only concurrent connection), 기존 KHDP storage layer 재사용
- **배포**: 로컬 모드(stdio)와 원격 모드(HTTP+SSE) 둘 다 지원
- **컨테이너**: 기존 Firecracker/gVisor 샌드박스 정책과 동일한 격리 수준

## Tier 2: 벤더 중립 가이드 (KHDP_AGENT_GUIDE.md)

OpenAI 진영의 `AGENTS.md`, Anthropic 진영의 `CLAUDE.md` 양쪽 모두에서 자연스럽게 참조되도록 작성.

### 포함 내용

- KHDP MCP 도구 카탈로그 요약
- 도메인 워크플로우 (예시)
  - "OMOP 코호트 분석 시: `find_concept` → `describe_table` → `sample_rows` → `query` 순서. 단일 SQL로 끝내려 하지 말고 코딩 에이전트 루프 활용."
  - "데이터셋 업로드 전: 반드시 PHI 스캔 도구로 검증. manifest는 자동 생성됨."
  - "결과를 논문/IRB 보고서에 인용할 경우 `khdp_result_pin` 필수."
- 안 되는 것 명시 (negative examples)
  - 의료 판단·진단·생성 표현 회피 (UI 카피 정책과 동일)
  - 환자 식별자 평문 출력 금지
  - egress 한도를 우회하려는 다단 쿼리 분해 금지

### 배포 위치

- KHDP 공식 문서 사이트 (`docs.khdp.net/agent-guide`)
- 프로젝트 워크스페이스 루트에 자동 배치되는 옵션 제공 (`khdp init` 시)

## Tier 3: 벤더별 자산 (선택적)

### Anthropic Skill

- `khdp-omop-analysis/SKILL.md` — OMOP 분석 워크플로우 자동 트리거
- `khdp-dataset-management/SKILL.md` — 데이터셋 입출력 워크플로우
- 보조 스크립트(검증·정규화)를 함께 패키징
- Claude Code 사용자에게 자동 발견·로드되는 편의 기능

### 기타 (필요 시)

- Cursor `.cursorrules`, Windsurf rules 등 IDE별 설정 어댑터
- OpenAI Custom GPT용 OpenAPI 스펙 (MCP 서버를 HTTP로 노출 시 자동 생성 가능)

## 단계별 로드맵

### Phase 0: 설계 확정 (현재)
- [ ] 노출 도구 시그니처 확정 (위 초안 검토)
- [ ] 인증 모델 결정: OAuth scope 정의, refresh token 저장 정책
- [ ] 감사 로그 스키마 확정 (기존 audit log와 통합)
- [ ] MCP transport 결정: 로컬 stdio 우선 / 원격 HTTP는 후속

### Phase 1: MVP MCP 서버
- [ ] 인증 도구 (`auth_status`, `auth_login`, `auth_logout`)
- [ ] 읽기 전용 도구 (`dataset_list`, `dataset_describe`, `omop_describe_table`)
- [ ] 단일 사용자, stdio transport, 로컬 실행
- [ ] 단위 테스트 + audit log smoke test

### Phase 2: 데이터 입출력
- [ ] `dataset_download` (AES-GCM URL 발급 통합)
- [ ] `dataset_upload` (PHI 스캔, manifest 자동 생성)
- [ ] 토큰 갱신 자동화

### Phase 3: OMOP 분석 도구
- [ ] `omop_find_concept`, `omop_sample_rows`, `omop_query`
- [ ] k-anonymity egress 정책 통합
- [ ] DuckDB read-only concurrent 연결 풀

### Phase 4: 재현성 + 다중 사용자
- [ ] `khdp_result_pin` (결과 스냅샷)
- [ ] 원격 HTTP transport (다중 사용자 시나리오)
- [ ] per-user credential 격리 검증

### Phase 5: Tier 2/3 자산
- [ ] `KHDP_AGENT_GUIDE.md` 작성·배포
- [ ] Anthropic Skill 패키징
- [ ] 외부 에이전트 호환성 검증 (Claude Code, Codex CLI, OpenCode, Cursor 각각)

## 결정 필요 항목

### 1. 인증 backend
- (a) infmedix 자체 OAuth provider 구축 (KCMVP 모듈 OEM 라인 활용)
- (b) 기존 SNUH SSO 연동
- (c) 단기적으로 API key + 장기적으로 OAuth 마이그레이션
- → 1차 의견: (c). MVP 부담을 줄이고, OAuth는 다중 사용자 단계(Phase 4)에서 도입.

### 2. MCP 서버 배포 위치
- (a) 사용자 로컬 머신 (stdio)
- (b) snuh.ai 서버 사이드 (HTTP+SSE)
- (c) 둘 다 지원
- → 1차 의견: (c). MVP는 (a)로 시작, Phase 4에서 (b) 추가. 로컬은 단일 연구자 자동화에, 원격은 IRB 감사·다중 사용자 환경에.

### 3. 의료 데이터 egress 정책
- MCP 도구가 반환하는 데이터의 row limit, 컬럼 마스킹, k-anonymity 임계값을 어디서 결정할 것인가
- → MCP 서버가 아닌 KHDP backend의 기존 egress control layer에서 강제. MCP는 단순 패스스루.

### 4. 외부 호환성 검증 범위
- 어느 에이전트까지 1차 지원 대상으로 명시할 것인가
- → 1차 의견: Claude Code, Codex CLI, OpenCode를 우선. Cursor·Windsurf는 best-effort.

## 위험 요소

- **MCP 표준 변동성**: 2024–2026년 동안 빠르게 진화 중. transport, auth, 권한 모델이 바뀔 여지. → 추상화 레이어로 격리, SDK 버전 핀.
- **다중 LLM의 도구 호출 비결정성**: 같은 MCP 도구를 모델마다 다르게 호출. → Tier 2 가이드 + 도구 시그니처 엄격화로 완충.
- **OAuth 부트스트랩 UX**: 의료 연구자(비개발자)에게 loopback OAuth는 낯섦. → CLI 한 줄 명령(`khdp login`)으로 캡슐화.
- **Skill vs MCP 중복 유지보수**: Tier 1과 Tier 3가 중복 진화하지 않도록 Tier 3는 워크플로우(가이드)에만 집중하고 도구 자체는 절대 재구현하지 않음.

## 참고

- MCP 사양: https://modelcontextprotocol.io
- RFC 8252 (OAuth for Native Apps), RFC 7636 (PKCE)
- Anthropic Agent Skills 문서
- KHDP 기존 설계 문서: AES-GCM URL 스킴, manifest access control, k-anonymity egress
