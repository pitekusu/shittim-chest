import Ajv2020 from "ajv/dist/2020";

import recordsApiSchema from "../../../contracts/records/v1/records-api.schema.json";

const validator = new Ajv2020({ allErrors: true, strict: true, validateFormats: false }).compile(
  recordsApiSchema,
);

export function isRecordsApiResponse(value: unknown): boolean {
  return validator(value);
}
