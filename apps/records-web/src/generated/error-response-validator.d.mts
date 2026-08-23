interface ValidationError {
  readonly instancePath: string;
  readonly schemaPath: string;
  readonly keyword: string;
  readonly message?: string;
  readonly params: Readonly<Record<string, unknown>>;
}

declare function validate(value: unknown): boolean;

declare namespace validate {
  let errors: readonly ValidationError[] | null;
}

export default validate;
