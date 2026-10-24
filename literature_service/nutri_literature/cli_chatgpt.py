"""Personal-account companion: official login, resumable comparison and local UI."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="공식 개인 ChatGPT 연결·동일 기준 비교")
    sub = parser.add_subparsers(dest="command", required=True)
    login = sub.add_parser("connect", help="다른 계정으로 공식 로그인")
    login.add_argument("--account", default=None, help="저장 계정 재인증 ID. 생략하면 새 계정")
    sub.add_parser("status")
    sub.add_parser("models")
    serve = sub.add_parser("serve", help="127.0.0.1 전용 개인 연결 화면")
    serve.add_argument("--port", type=int, default=8521)
    serve.add_argument("--home", type=Path, required=True)
    serve.add_argument("--output", type=Path, required=True)
    run = sub.add_parser("compare", help="로그인한 계정으로 동일 100편 비교")
    run.add_argument("--home", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--model", required=True)
    run.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], default="medium")
    args = parser.parse_args()
    from .chatgpt_auth import ChatGPTAuth, ChatGPTAuthError
    from .chatgpt_client import available_models, ChatGPTRequestError
    auth = ChatGPTAuth()
    try:
        if args.command == "connect":
            attempt = auth.start_sign_in(args.account, timeout=1800)
            print(json.dumps({"status": "waiting_for_user", "new_account": args.account is None}), flush=True)
            result = attempt.wait(1805)
            print(json.dumps({"status": "connected", "account": result}, ensure_ascii=False), flush=True)
        elif args.command == "status":
            print(json.dumps(auth.status(), ensure_ascii=False))
        elif args.command == "models":
            print(json.dumps(available_models(auth), ensure_ascii=False))
        elif args.command == "serve":
            import uvicorn
            from .chatgpt_companion import create_companion
            app = create_companion(args.home, args.output, auth=auth, port=args.port)
            uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, log_level="warning")
        elif args.command == "compare":
            from .chatgpt_batch import batch_lock
            from .chatgpt_prefetch import PrefetchedComparison
            from .chatgpt_evaluation import prepare_experiment, run_experiment
            from .chatgpt_rescore import score_corrected_experiment
            profile = auth.status()["active_registration_id"]
            models = available_models(auth, profile)
            if args.model not in {m["slug"] for m in models}:
                raise ChatGPTRequestError("선택 모델이 연결 계정의 공식 목록에 없습니다.")
            with batch_lock(args.output.parent):
                prepare_experiment(args.home, args.home / "baseline", args.output, args.model,
                                   dictionary_path=args.home / "official-id-dictionary.json.gz", reasoning_effort=args.effort)
                with PrefetchedComparison(auth, args.output, registration_id=profile) as inference:
                    result = run_experiment(args.output, inference)
            if result["completed_papers"] != 100:
                print(json.dumps(result, ensure_ascii=False))
                return 2
            print(json.dumps(score_corrected_experiment(args.output, args.home), ensure_ascii=False))
    except (ChatGPTAuthError, ChatGPTRequestError) as error:
        print(json.dumps({"status": "attention_required", "message": str(error)}, ensure_ascii=False))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
