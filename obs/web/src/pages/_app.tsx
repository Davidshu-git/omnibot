import type { AppProps } from "next/app";
import Head from "next/head";
import { useState, useCallback } from "react";
import Layout from "@/components/Layout";
import { FocusModeContext } from "@/lib/focus-mode-context";
import "@/styles/globals.css";

export default function App({ Component, pageProps }: AppProps) {
  const [focus, setFocus] = useState(false);
  const setFocusMode = useCallback((v: boolean | ((prev: boolean) => boolean)) => {
    setFocus((prev) => typeof v === "function" ? v(prev) : v);
  }, []);

  return (
    <>
      <Head>
        <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
      </Head>
      <FocusModeContext.Provider value={{ focus, setFocus: setFocusMode }}>
        <Layout>
          <Component {...pageProps} />
        </Layout>
      </FocusModeContext.Provider>
    </>
  );
}
