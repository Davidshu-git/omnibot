import { Html, Head, Main, NextScript } from "next/document";

export default function Document() {
  return (
    <Html lang="zh-CN">
      <Head>
        <link rel="icon" type="image/png" sizes="32x32" href="/favicon-32x32.png?v=20260525" />
        <link rel="shortcut icon" href="/favicon.ico?v=20260525" />
        <link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png?v=20260525" />
      </Head>
      <body>
        <Main />
        <NextScript />
      </body>
    </Html>
  );
}
