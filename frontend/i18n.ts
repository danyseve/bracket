import i18n from 'i18next';
import LanguageDetector from 'i18next-browser-languagedetector';
import Backend from 'i18next-http-backend';
import { initReactI18next } from 'react-i18next';

import { i18nInitOptions } from './i18n_options';

i18n.use(Backend).use(LanguageDetector).use(initReactI18next).init(i18nInitOptions);

export default i18n;
