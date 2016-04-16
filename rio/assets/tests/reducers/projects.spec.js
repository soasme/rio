import expect from 'expect'

import {
  NEW_PROJECT_SUCCESS,
  NEW_PROJECT_FAILURE,
  projects
} from '../../reducers/projects'

describe('project reducer', () => {

  it('should attach new project into projects', () => {
    let originState = new Map()
    let action = {type: NEW_PROJECT_SUCCESS, payload: {id: 1, slug: 'new-project'}}

    expect(
      projects(originState, action).get(1)
    ).toEqual(
      {id: 1, slug: 'new-project'}
    )
  })


})
