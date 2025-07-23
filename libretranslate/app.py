import os
import tempfile
from html import unescape

from flask import Blueprint, Flask, abort, jsonify, request, session
from flask_babel import Babel
from flask_session import Session

from libretranslate import flood, remove_translated_files, scheduler, secret, storage
from libretranslate.language import iso2model, improve_translation_formatting

# Rough map of emoji characters
emojis = {e: True for e in \
  [ord(' ')] +                    # Spaces
  list(range(0x1F600,0x1F64F)) +  # Emoticons
  list(range(0x1F300,0x1F5FF)) +  # Misc Symbols and Pictographs
  list(range(0x1F680,0x1F6FF)) +  # Transport and Map
  list(range(0x2600,0x26FF)) +    # Misc symbols
  list(range(0x2700,0x27BF)) +    # Dingbats
  list(range(0xFE00,0xFE0F)) +    # Variation Selectors
  list(range(0x1F900,0x1F9FF)) +  # Supplemental Symbols and Pictographs
  list(range(0x1F1E6,0x1F1FF)) +  # Flags
  list(range(0x20D0,0x20FF))      # Combining Diacritical Marks for Symbols
}

def get_version():
    try:
        with open("VERSION") as f:
            return f.read().strip()
    except:
        return "?"


def get_upload_dir():
    upload_dir = os.path.join(tempfile.gettempdir(), "libretranslate-files-translate")

    if not os.path.isdir(upload_dir):
        os.mkdir(upload_dir)

    return upload_dir


def get_req_api_key():
    if request.is_json:
        json = get_json_dict(request)
        ak = json.get("api_key")
    else:
        ak = request.values.get("api_key")

    return ak

def get_req_secret():
    if request.is_json:
        json = get_json_dict(request)
        ak = json.get("secret")
    else:
        ak = request.values.get("secret")

    return ak


def get_json_dict(request):
    d = request.get_json()
    if not isinstance(d, dict):
        abort(400, description="Invalid JSON format")
    return d


def get_remote_address():
    if request.headers.getlist("X-Forwarded-For"):
        ip = request.headers.getlist("X-Forwarded-For")[0].split(",")[0]
    else:
        ip = request.remote_addr or "127.0.0.1"

    return ip

def get_fingerprint():
    return request.headers.get("User-Agent", "") + request.headers.get("Cookie", "")


def get_req_limits(default_limit, api_keys_db, db_multiplier=1, multiplier=1):
    req_limit = default_limit

    if api_keys_db:
        api_key = get_req_api_key()

        if api_key:
            api_key_limits = api_keys_db.lookup(api_key)
            if api_key_limits is not None:
                req_limit = api_key_limits[0] * db_multiplier

    return int(req_limit * multiplier)


def get_char_limit(default_limit, api_keys_db):
    char_limit = default_limit

    if api_keys_db:
        api_key = get_req_api_key()

        if api_key:
            api_key_limits = api_keys_db.lookup(api_key)
            if api_key_limits is not None:
                if api_key_limits[1] is not None:
                    char_limit = api_key_limits[1]

    return char_limit


def get_routes_limits(args, api_keys_db):
    default_req_limit = args.req_limit
    if default_req_limit == -1:
        # TODO: better way?
        default_req_limit = 9999999999999

    def minute_limits():
        return "%s per minute" % get_req_limits(default_req_limit, api_keys_db)

    def hourly_limits(n):
        def func():
          decay = (0.75 ** (n - 1))
          return "{} per {} hour".format(get_req_limits(args.hourly_req_limit * n, api_keys_db, int(os.environ.get("LT_HOURLY_REQ_LIMIT_MULTIPLIER", 60) * n), decay), n)
        return func

    def daily_limits():
        return "%s per day" % get_req_limits(args.daily_req_limit, api_keys_db, int(os.environ.get("LT_DAILY_REQ_LIMIT_MULTIPLIER", 1440)))

    res = [minute_limits]

    if args.hourly_req_limit > 0:
      for n in range(1, args.hourly_req_limit_decay + 2):
        res.append(hourly_limits(n))

    if args.daily_req_limit > 0:
        res.append(daily_limits)

    return res

def filter_unique(seq, extra):
    seen = set({extra, ""})
    seen_add = seen.add
    return [x for x in seq if not (x in seen or seen_add(x))]


def detect_translatable(src_texts):
  if isinstance(src_texts, list):
    return any(detect_translatable(t) for t in src_texts)
  
  for ch in src_texts:
    if not (ord(ch) in emojis):
      return True
  
  # All emojis
  return False


def create_app(args):
    from libretranslate.init import boot

    boot(args.load_only, args.update_models, args.force_update_models)

    from libretranslate.language import load_languages

    bp = Blueprint('Main app', __name__)

    storage.setup(args.shared_storage)

    if not args.disable_files_translation:
        remove_translated_files.setup(get_upload_dir())
    languages = load_languages()
    language_pairs = {}
    for lang in languages:
        language_pairs[lang.code] = sorted([l.to_lang.code for l in lang.translations_from])

    # Map userdefined frontend languages to argos language object.
    if args.frontend_language_source == "auto":
        frontend_argos_language_source = type(
            "obj", (object,), {"code": "auto", "name": "Auto Detect"}
        )
    else:
        frontend_argos_language_source = next(
            iter([l for l in languages if l.code == args.frontend_language_source]),
            None,
        )
    if frontend_argos_language_source is None:
        frontend_argos_language_source = languages[0]
    
    if not "gunicorn" in os.environ.get("SERVER_SOFTWARE", ""):
      # Gunicorn starts the scheduler in the master process
      scheduler.setup(args)

    flood.setup(args)
    secret.setup(args)

    @bp.errorhandler(400)
    def invalid_api(e):
        return jsonify({"error": str(e.description)}), 400

    @bp.errorhandler(500)
    def server_error(e):
        return jsonify({"error": str(e.description)}), 500

    @bp.errorhandler(429)
    def slow_down_error(e):
        flood.report(get_remote_address())
        return jsonify({"error": "Slowdown:" + " " + str(e.description)}), 429

    @bp.errorhandler(403)
    def denied(e):
        return jsonify({"error": str(e.description)}), 403

    @bp.route("/")
    def index():
        abort(404)

    # Add cors
    @bp.after_request
    def after_request(response):
        response.headers.add("Access-Control-Allow-Origin", "*")
        response.headers.add(
            "Access-Control-Allow-Headers", "Authorization, Content-Type"
        )
        response.headers.add("Access-Control-Expose-Headers", "Authorization")
        response.headers.add("Access-Control-Allow-Methods", "GET, POST")
        response.headers.add("Access-Control-Allow-Credentials", "true")
        response.headers.add("Access-Control-Max-Age", 60 * 60 * 24 * 20)
        return response

    @bp.post("/translate")
    def translate():
        """
        Translate Text
        ---
        tags:
          - translate
        parameters:
          - in: formData
            name: q
            schema:
              oneOf:
                - type: string
                  example: Hello world!
                - type: array
                  example: ['Hello world!']
            required: true
            description: Text(s) to translate
          - in: formData
            name: source
            schema:
              type: string
              example: en
            required: true
            description: Source language code or "auto" for auto detection
          - in: formData
            name: target
            schema:
              oneOf:
                - type: string
                  example: es
                - type: array
                  example: ['es', 'fr']
            required: true
            description: Target language code
          - in: formData
            name: format
            schema:
              type: string
              enum: [text, html]
              default: text
              example: text
            required: false
            description: >
              Format of source text:
               * `text` - Plain text
               * `html` - HTML markup
          - in: formData
            name: alternatives
            schema:
              type: integer
              default: 0
              example: 3
            required: false
            description: Preferred number of alternative translations 
          - in: formData
            name: api_key
            schema:
              type: string
              example: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
            required: false
            description: API key
        responses:
          200:
            description: Translation
            schema:
              id: translate
              type: object
              properties:
                translatedText:
                  oneOf:
                    - type: string
                    - type: array
                  description: Translated text(s)
                detectedLanguage:
                  oneOf:
                    - type: object
                      properties:
                        confidence:
                          type: number
                          format: float
                          minimum: 0
                          maximum: 100
                          description: Confidence value
                          example: 100
                        language:
                          type: string
                          description: Language code
                    - type: array
                      items:
                        type: object
                        properties:
                          confidence:
                            type: number
                            format: float
                            minimum: 0
                            maximum: 100
                            description: Confidence value
                            example: 100
                          language:
                            type: string
                            description: Language code
                alternatives:
                  oneOf:
                    - type: array
                      items:
                        type: string
                    - type: array
                      items:
                        type: array
                        items:
                          type: string
                  description: Alternative translations
              required:
                - translatedText
          400:
            description: Invalid request
            schema:
              id: error-response
              type: object
              properties:
                error:
                  type: string
                  description: Error message
          500:
            description: Translation error
            schema:
              id: error-response
              type: object
              properties:
                error:
                  type: string
                  description: Error message
          429:
            description: Slow down
            schema:
              id: error-slow-down
              type: object
              properties:
                error:
                  type: string
                  description: Reason for slow down
          403:
            description: Banned
            schema:
              id: error-response
              type: object
              properties:
                error:
                  type: string
                  description: Error message
        """
        if request.is_json:
            json = get_json_dict(request)
            q = json.get("q")
            source_lang = iso2model(json.get("source"))
            target_lang = iso2model(json.get("target"))
            num_alternatives = int(json.get("alternatives", 0))
        else:
            q = request.values.get("q")
            source_lang = iso2model(request.values.get("source"))
            target_lang = iso2model(request.values.get("target"))
            num_alternatives = request.values.get("alternatives", 0)

        if not q:
            abort(400, description=f"Invalid request: missing `q` parameter")
        if not source_lang:
            abort(400, description=f"Invalid request: missing `source` parameter")
        if not target_lang:
            abort(400, description=f"Invalid request: missing `target` parameter")
        
        try:
            num_alternatives = max(0, int(num_alternatives))
        except ValueError:
            abort(400, description=f"Invalid request: `alternatives` parameter is not a number")

        if not request.is_json:
            q = "\n".join(q.splitlines())

        src_texts = q if isinstance(q, list) else [q]

        request.req_cost = max(1, len(q))

        translatable = detect_translatable(src_texts)
        if translatable:
            detected_src_lang = {"confidence": 100.0, "language": source_lang}
        else:
            detected_src_lang = {"confidence": 0.0, "language": "en"}
        
        src_lang = next(iter([l for l in languages if l.code == detected_src_lang["language"]]), None)

        if src_lang is None:
            abort(400, description=f"{source_lang}s is not supported")

        try:
            targets = []
            if isinstance(target_lang, list):
                targets = [l for l in languages if l.code in target_lang]
            else:
                targets = [l for l in languages if l.code == target_lang]

            if not targets:
                abort(400, description=f"{target_lang}s is not supported")
            
            batch_results = {}
            batch_alternatives = []
            for tgt_lang in targets:
                batch_results[tgt_lang.code] = []
                for text in src_texts:
                    translator = src_lang.get_translation(tgt_lang)
                    if translator is None:
                        abort(400, description=f"{tgt_lang.name}s ({tgt_lang.code}s) is not available as a target language from {src_lang.name}s ({src_lang.code}s)")

                    if translatable:
                        hypotheses = translator.hypotheses(text, num_alternatives + 1)
                        translated_text = unescape(improve_translation_formatting(text, hypotheses[0].value))
                        alternatives = filter_unique([unescape(improve_translation_formatting(text, hypotheses[i].value)) for i in range(1, len(hypotheses))], translated_text)
                    else:
                        translated_text = text # Cannot translate, send the original text back
                        alternatives = []

                    batch_results[tgt_lang.code].append(translated_text)
                    batch_alternatives.append(alternatives)
            
            result = {"translatedText": batch_results}

            return jsonify(result)
        except Exception as e:
            abort(500, description=f"Cannot translate text: {str(e)}s")
            raise e

    app = Flask(__name__)

    app.config["SESSION_TYPE"] = "filesystem"
    app.config["SESSION_FILE_DIR"] = os.path.join("db", "sessions")
    app.config["JSON_AS_ASCII"] = False
    Session(app)

    if args.debug:
        app.config["TEMPLATES_AUTO_RELOAD"] = True
    if args.url_prefix:
        app.register_blueprint(bp, url_prefix=args.url_prefix)
    else:
        app.register_blueprint(bp)

    app.config["BABEL_TRANSLATION_DIRECTORIES"] = 'locales'

    Babel(app)

    return app
