import React,{createContext,useContext,useEffect,useState} from "react";

export type Language="ja"|"en";
const LanguageContext=createContext<{language:Language;setLanguage:(language:Language)=>void}>({language:"ja",setLanguage:()=>{}});
export function LanguageProvider({children}:{children:React.ReactNode}){
  const [language,setLanguageState]=useState<Language>(()=>localStorage.getItem("olcr-language")==="en"?"en":"ja");
  useEffect(()=>{document.documentElement.lang=language;},[language]);
  const setLanguage=(next:Language)=>{localStorage.setItem("olcr-language",next);setLanguageState(next);document.documentElement.lang=next;};
  return <LanguageContext.Provider value={{language,setLanguage}}>{children}</LanguageContext.Provider>;
}
export function useLanguage(){return useContext(LanguageContext)}
