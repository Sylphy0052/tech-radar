/**
 * テストの持ち時間に `TEST_TIMEOUT_MS` を明示させる ESLint ルール（Issue #47）。
 *
 * `src/test-utils/timeouts.ts` が記録しているとおり、この定数を明示的に付けるか
 * どうかが「持ち時間を意識したかどうか」の印になる。ただし規約として書いてある
 * だけでは、新しいテストを足すときに持ち時間を省略しても誰も気づかず、そのテスト
 * だけが既定の5秒に静かに戻る。実際 Issue #40 の作業中に、別ブランチで追加された
 * テストファイルが未適用のまま入っていた。人の記憶ではなく lint で止める。
 *
 * 数値の直書き（`it("...", fn, 5000)`）も違反として扱う。個別に数字を置けるように
 * すると、`timeouts.ts` へ集約した根拠が再び各ファイルへ散らばるため。別の持ち時間
 * がどうしても要るテストは `eslint-disable-next-line` で意図を明示させる。
 *
 * vitest が受け付ける形は2通りある（`@vitest/runner` の `TestCollectorCallable` /
 * `EachFunctionReturn` / `TestForFunctionReturn`）。
 *
 * - `it("...", fn, TEST_TIMEOUT_MS)` — 位置引数。`it` 本体と `it.each` が取る
 * - `it("...", { timeout: TEST_TIMEOUT_MS }, fn)` — オプション形式。上に加え `it.for` も取る
 *
 * `it.for` は位置引数の形を型として持たない。渡しても実行時に無視され、既定の5秒へ
 * 静かに戻る（このルールが防ごうとしている状態そのもの）ため、違反として報告する。
 *
 * どの呼び出しが実際のテスト定義かは「第1引数がテスト名か」で見分ける。`it.each(...)`
 * `it.for(...)` `it.skipIf(...)` `test.extend(...)` のように、いったん呼んでから
 * その戻り値を呼ぶ形が vitest には複数あり、修飾子の名前を並べた許可リストで
 * 見分けようとすると、API が増えるたびに誤検出する。テスト名を第1引数に取るのは
 * 最後の呼び出しだけなので、そこを手がかりにすれば形が増えても壊れない。
 *
 * 既知の限界（いずれも意図して検査対象から外している）:
 *
 * - 持ち時間が `TEST_TIMEOUT_MS` という名前かどうかしか見ない。同名のローカル変数を
 *   宣言して渡せば通るし、逆に `{ timeout }` のショートハンドは名前が違うため弾く。
 *   束縛まで辿るとルールが重くなるうえ、そこまでして規約を迂回する書き方は lint では
 *   なくレビューで気づくべきものと判断した
 * - テスト名を変数で渡す呼び出し（`it(caseName, fn)`）は、テスト定義だと見分けられない
 * - `it` を別名で import した場合（`import { it as vIt }`）や、計算プロパティ経由の
 *   呼び出し（`it["skip"](...)`）は検出しない
 * - 引数にスプレッドが混ざる呼び出しや、テスト本体が関数リテラルでない呼び出しは、
 *   どれが持ち時間の位置なのかを静的に決められないため対象外
 */

const TIMEOUT_IDENTIFIER = "TEST_TIMEOUT_MS";
const TIMEOUT_OPTION_KEY = "timeout";
const TEST_FUNCTIONS = new Set(["it", "test"]);
// 本体を取らないため持ち時間の概念がない修飾子。
const BODYLESS_MODIFIER = "todo";
// `it.for` はオプション形式しか取らない（`it.each` と違い位置引数の型を持たない）。
const OPTIONS_ONLY_MODIFIER = "for";
const FUNCTION_LITERALS = new Set(["FunctionExpression", "ArrowFunctionExpression"]);

/**
 * 呼び出し対象の式を分解し、根になる識別子と経由した修飾子を返す。
 *
 * `it.skip.each([...])(...)` のような連なりも、`it` まで遡って一様に扱えるようにする。
 * 解釈できない形（計算プロパティなど）は null を返し、対象外として扱う。
 */
function analyzeCallee(node) {
  if (node.type === "Identifier") {
    return { root: node.name, modifiers: [] };
  }
  if (node.type === "MemberExpression" && !node.computed && node.property.type === "Identifier") {
    const inner = analyzeCallee(node.object);
    return inner === null
      ? null
      : { root: inner.root, modifiers: [...inner.modifiers, node.property.name] };
  }
  // `it.each([...])(...)` や `` it.each`table`(...) `` のように、呼び出しの戻り値を
  // さらに呼ぶ形。修飾子だけ引き継ぐ。
  if (node.type === "CallExpression") {
    return analyzeCallee(node.callee);
  }
  if (node.type === "TaggedTemplateExpression") {
    return analyzeCallee(node.tag);
  }
  return null;
}

/** 第1引数がテスト名（文字列）かを判定する。 */
function isTestName(node) {
  return (
    (node.type === "Literal" && typeof node.value === "string") || node.type === "TemplateLiteral"
  );
}

/**
 * 持ち時間を渡すべきテスト定義の呼び出しなら、その形の情報を返す。
 * 対象外なら null を返す。
 */
function analyzeTestCall(node) {
  const info = analyzeCallee(node.callee);
  if (info === null || !TEST_FUNCTIONS.has(info.root)) {
    return null;
  }
  if (info.modifiers.includes(BODYLESS_MODIFIER)) {
    return null;
  }
  const name = node.arguments[0];
  if (name === undefined || !isTestName(name)) {
    return null;
  }
  return { optionsOnly: info.modifiers.includes(OPTIONS_ONLY_MODIFIER) };
}

function isSharedTimeout(node) {
  return node.type === "Identifier" && node.name === TIMEOUT_IDENTIFIER;
}

/** プロパティのキー名を返す。静的に決められないキーは null。 */
function staticKeyName(property) {
  const key = property.key;
  if (key.type === "Identifier" && !property.computed) {
    return key.name;
  }
  if (key.type === "Literal") {
    return key.value;
  }
  return null;
}

/**
 * オプションオブジェクトから `timeout` の値を取り出す。
 *
 * - 見つかった → その値（明示されていれば、他にスプレッドがあっても検査する）
 * - 見つからず、静的に決められない要素も無い → undefined（指定漏れ）
 * - 見つからず、スプレッドや動的キーがある → null（判定不能なので対象外）
 */
function findTimeoutOption(objectExpression) {
  let value;
  let hasUnknownMember = false;

  for (const property of objectExpression.properties) {
    if (property.type !== "Property") {
      hasUnknownMember = true;
      continue;
    }
    const name = staticKeyName(property);
    if (name === null) {
      hasUnknownMember = true;
      continue;
    }
    if (name === TIMEOUT_OPTION_KEY) {
      value = property.value;
    }
  }

  if (value !== undefined) {
    return value;
  }
  return hasUnknownMember ? null : undefined;
}

/** @type {import("eslint").Rule.RuleModule} */
const requireTestTimeout = {
  meta: {
    type: "problem",
    docs: {
      description: `テストの持ち時間に ${TIMEOUT_IDENTIFIER} を明示させる`,
    },
    schema: [],
    messages: {
      missingTimeout: `テストの持ち時間が指定されていません。${TIMEOUT_IDENTIFIER} を渡してください（付けないと vitest 既定の5000msになり、高負荷時にだけ落ちます）。`,
      useSharedTimeout: `テストの持ち時間には ${TIMEOUT_IDENTIFIER} を使ってください。別の値がどうしても要る場合は eslint-disable-next-line で理由を明示してください。`,
      useOptionsFormat: `it.for / test.for は持ち時間を位置引数で受け取らず、渡しても無視されます。{ timeout: ${TIMEOUT_IDENTIFIER} } をテスト本体の前に渡してください。`,
    },
  },
  create(context) {
    return {
      CallExpression(node) {
        const testCall = analyzeTestCall(node);
        if (testCall === null) {
          return;
        }
        if (node.arguments.some((argument) => argument.type === "SpreadElement")) {
          return;
        }

        const second = node.arguments[1];
        if (second !== undefined && second.type === "ObjectExpression") {
          const timeout = findTimeoutOption(second);
          if (timeout === null) {
            return;
          }
          if (timeout === undefined) {
            context.report({ node: second, messageId: "missingTimeout" });
            return;
          }
          if (!isSharedTimeout(timeout)) {
            context.report({ node: timeout, messageId: "useSharedTimeout" });
          }
          return;
        }

        if (testCall.optionsOnly) {
          context.report({ node, messageId: "useOptionsFormat" });
          return;
        }
        if (second !== undefined && !FUNCTION_LITERALS.has(second.type)) {
          // テスト本体が関数リテラルでない。第3引数を持ち時間と決めつけられない。
          return;
        }

        const timeout = node.arguments[2];
        if (timeout === undefined) {
          context.report({ node, messageId: "missingTimeout" });
          return;
        }
        if (!isSharedTimeout(timeout)) {
          context.report({ node: timeout, messageId: "useSharedTimeout" });
        }
      },
    };
  },
};

export default requireTestTimeout;
