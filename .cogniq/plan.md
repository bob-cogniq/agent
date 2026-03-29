파일 쓰기 권한이 필요합니다. 허용해 주시면 `plan.md`를 작성하겠습니다.

작성할 내용을 미리 보여드리면:

**현황 파악 요약:**
- 이메일+비밀번호 로그인, JWT, `get_current_user` → **이미 완성**
- Google OAuth, 비밀번호 재설정 → **미구현**

**7개 태스크 (의존성 순서):**
1. `pyproject.toml` — `aiosmtplib`, `itsdangerous` 추가
2. `config.py` — Google OAuth + SMTP + app_base_url 설정 추가
3. `auth/models.py` — `UserInDB`에 `oauth_provider`, `oauth_sub` 필드 추가
4. `api/schemas.py` — `ForgotPasswordRequest`, `ResetPasswordRequest` 스키마 추가
5. `auth/email_service.py` (신규) — SMTP 발송, 미설정 시 로그 대체
6. `auth/oauth.py` (신규) — `build_google_auth_url`, `exchange_code_for_userinfo` (httpx 사용)
7. `auth/router.py` — `GET /google`, `GET /google/callback`, `POST /forgot-password`, `POST /reset-password` 추가

**핵심 설계 결정:**
- OAuth state CSRF 방지: `itsdangerous.TimestampSigner` (5분 만료)
- 비밀번호 재설정 토큰: 동일 `TimestampSigner` (1시간 만료), DB 저장 불필요
- `forgot-password`는 이메일 미존재 시에도 204 반환

파일 쓰기를 허용해 주시면 바로 저장하겠습니다.