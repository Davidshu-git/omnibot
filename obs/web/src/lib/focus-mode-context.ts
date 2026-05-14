import { createContext, useContext } from "react";

export const FocusModeContext = createContext<{
  focus: boolean;
  setFocus: (v: boolean | ((prev: boolean) => boolean)) => void;
}>({ focus: false, setFocus: () => {} });

export const useFocusMode = () => useContext(FocusModeContext);
